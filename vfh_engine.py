# -*- coding: utf-8 -*-
"""VFH经营决策引擎。

模型把实际SKU主数据与用户可编辑的情景假设分开：SKU数量、尺寸来自本地数据库；
缺失的批次库龄、费率和未来业务量只能作为情景输入，不会被标记为实际观测。
"""
from __future__ import annotations

import math
import random
from datetime import date, timedelta


DATA_DENSITY = {
    'detailed': {'label': '详细数据', 'noise': 0.08, 'band': 8},
    'warning': {'label': '预警数据', 'noise': 0.18, 'band': 18},
    'sparse': {'label': '稀疏数据', 'noise': 0.30, 'band': 30},
}


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _round_money(value):
    return round(float(value), 2)


def _p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]


def _b_capacity(area_m2, scenario, settings):
    office = _num(scenario.get('office_percent'), 5) / 100
    rest = _num(scenario.get('rest_percent'), 5) / 100
    usable_area = max(0, area_m2 * (1 - office - rest))
    aisle_share = _num(settings.get('aisle_percent'), 30) / 100
    floor_share = _num(settings.get('floor_percent'), 30) / 100
    rack_share = max(0, 1 - aisle_share - floor_share)
    floor_area = usable_area * floor_share
    rack_area = usable_area * rack_share
    shelf_footprint = max(
        0.2,
        _num(settings.get('shelf_width_m'), 2.7)
        * _num(settings.get('shelf_depth_m'), 1.1)
        * 1.18,
    )
    rack_bays = math.floor(rack_area / shelf_footprint)
    rack_positions = rack_bays * max(1, int(settings.get('shelf_levels') or 1))
    floor_positions = math.floor(floor_area / (1.2 * 0.8 * 1.24))
    return {
        'gross_area_m2': round(area_m2, 1),
        'usable_area_m2': round(usable_area, 1),
        'office_area_m2': round(area_m2 * office, 1),
        'rest_area_m2': round(area_m2 * rest, 1),
        'aisle_area_m2': round(usable_area * aisle_share, 1),
        'floor_area_m2': round(floor_area, 1),
        'rack_area_m2': round(rack_area, 1),
        'rack_positions': rack_positions,
        'floor_positions': floor_positions,
        'capacity_pallets': rack_positions + floor_positions,
    }


def _sku_stats(skus):
    stocked = [sku for sku in skus if int(sku.get('qty') or 0) > 0]
    total_units = sum(int(sku.get('qty') or 0) for sku in stocked)
    known_volume_units = sum(
        int(sku.get('qty') or 0)
        for sku in stocked if _num(sku.get('volume_m3')) > 0
    )
    total_volume = sum(
        int(sku.get('qty') or 0) * _num(sku.get('volume_m3')) for sku in stocked
    )
    average_volume = total_volume / known_volume_units if known_volume_units else 0.08
    stock_pallets = sum(
        math.ceil(int(sku.get('qty') or 0) / max(1, int(sku.get('units_per_pallet') or 1)))
        for sku in stocked
    )
    average_units_per_pallet = total_units / stock_pallets if stock_pallets else 12
    return {
        'stocked': stocked,
        'total_units': total_units,
        'total_volume_m3': total_volume,
        'average_volume_m3': average_volume,
        'average_units_per_pallet': max(1, average_units_per_pallet),
        'stock_pallets': stock_pallets,
    }


def _initial_lots(stats, scenario, batches=None):
    if batches:
        today = date.today()
        sku_by_code = {sku.get('code'): sku for sku in stats['stocked']}
        lots = []
        for batch in batches:
            if str(batch.get('zone', 'A')).upper() != 'A':
                continue
            sku = sku_by_code.get(batch.get('sku_code'), {})
            try:
                receipt_day = (date.fromisoformat(str(batch.get('receipt_date'))) - today).days
            except ValueError:
                receipt_day = -int(_num(scenario.get('current_avg_age_days'), 42))
            lots.append({
                'sku': batch.get('sku_code', 'UNKNOWN'),
                'receipt_day': receipt_day,
                'units': int(batch.get('qty') or 0),
                'unit_volume_m3': _num(batch.get('unit_volume_m3')) or _num(sku.get('volume_m3')) or stats['average_volume_m3'],
                'source': 'imported_batch',
            })
        return sorted(lots, key=lambda item: item['receipt_day'])
    avg_age = int(_num(scenario.get('current_avg_age_days'), 42))
    density = scenario.get('data_density', 'warning')
    offsets = [-24, -8, 8, 24] if density != 'detailed' else [-12, -4, 4, 12]
    lots = []
    stocked = sorted(stats['stocked'], key=lambda item: int(item.get('qty') or 0), reverse=True)
    for index, sku in enumerate(stocked):
        qty = int(sku.get('qty') or 0)
        volume = _num(sku.get('volume_m3')) or stats['average_volume_m3']
        lots.append({
            'sku': sku.get('code', 'UNKNOWN'),
            'receipt_day': -max(0, avg_age + offsets[index % len(offsets)]),
            'units': qty,
            'unit_volume_m3': volume,
            'source': 'observed_qty_assumed_age',
        })
    return sorted(lots, key=lambda item: item['receipt_day'])


def _consume_fifo(lots, requested_units):
    remaining = max(0.0, requested_units)
    consumed = 0.0
    while remaining > 0.0001 and lots:
        take = min(remaining, lots[0]['units'])
        lots[0]['units'] -= take
        remaining -= take
        consumed += take
        if lots[0]['units'] <= 0.0001:
            lots.pop(0)
    return consumed


def _aged_storage_cost(lots, day, free_days, rate):
    return sum(
        lot['units'] * lot['unit_volume_m3'] * rate
        for lot in lots if day - lot['receipt_day'] > free_days
    )


def _daily_capacity(workers, forklifts, scenario, settings):
    labor_eff = max(0.2, _num(settings.get('labor_efficiency'), 80) / 100)
    forklift_eff = max(0.2, _num(settings.get('forklift_efficiency'), 80) / 100)
    minutes_per_pallet = max(2, _num(scenario.get('minutes_per_internal_pallet'), 11))
    labor_regular = workers * 8 * 60 * labor_eff / minutes_per_pallet
    forklift_regular = forklifts * 8 * 60 * forklift_eff / max(3, minutes_per_pallet * 0.68)
    regular = max(1, min(labor_regular, forklift_regular))
    overtime_limit = max(0, _num(scenario.get('overtime_limit_hours_day'), 2))
    overtime = regular * overtime_limit / 8
    return regular, overtime


def _arrival_schedule(horizon, containers_month):
    if containers_month <= 0:
        return {}
    interval = 30 / containers_month
    arrivals = {}
    index = 0
    while True:
        day = round(index * interval)
        if day >= horizon:
            break
        arrivals[day] = arrivals.get(day, 0) + 1
        index += 1
    return arrivals


def _simulate(mode, scenario, a_settings, b_settings, stats, seed, batches=None):
    rng = random.Random(seed + (11 if mode == 'vfh' else 3))
    horizon = int(_num(scenario.get('horizon_days'), 365))
    density = DATA_DENSITY.get(scenario.get('data_density'), DATA_DENSITY['warning'])
    monthly_demand = _num(scenario.get('monthly_demand_units'), 12000)
    annual_growth = _num(scenario.get('business_growth_pct'), 8) / 100
    inbound_containers = _num(scenario.get('inbound_containers_month'), 4)
    units_per_container = _num(scenario.get('units_per_container'), 3200)
    units_per_pallet = max(1, _num(scenario.get('planning_units_per_pallet')) or stats['average_units_per_pallet'])
    avg_units_order = max(1, _num(scenario.get('avg_units_order'), 2.2))
    arrivals = _arrival_schedule(horizon, inbound_containers)
    lots = _initial_lots(stats, scenario, batches)
    has_imported_batches = bool(batches)
    a_units = float(sum(lot['units'] for lot in lots)) if has_imported_batches else float(stats['total_units'])
    b_units = 0.0
    backlog = 0.0
    capacity = _b_capacity(_num(scenario.get('b_area_m2'), 1800), scenario, b_settings)
    target_util = _num(scenario.get('target_utilization_pct'), 85) / 100
    initial_b_units = 0.0
    if mode == 'vfh' and has_imported_batches:
        b_units = float(sum(
            int(batch.get('qty') or 0) for batch in batches
            if str(batch.get('zone', '')).upper() == 'B'
        ))
    elif mode == 'vfh':
        initial_b_units = min(
            a_units * 0.32,
            capacity['capacity_pallets'] * units_per_pallet * _num(scenario.get('b_initial_utilization_pct'), 52) / 100,
        )
        initial_b_units = _consume_fifo(lots, initial_b_units)
        a_units -= initial_b_units
        b_units = initial_b_units

    workers = int(_num(scenario.get('b_workers'), 3)) if mode == 'vfh' else int(_num(a_settings.get('workers'), 10))
    forklifts = int(_num(b_settings.get('forklifts'), 2)) if mode == 'vfh' else int(_num(a_settings.get('forklifts'), 3))
    regular_capacity, overtime_capacity = _daily_capacity(
        workers, forklifts, scenario, b_settings if mode == 'vfh' else a_settings
    )
    free_days = int(_num(scenario.get('a_free_days'), 60))
    storage_rate = _num(scenario.get('a_storage_rate_m3_day'), 0.022)
    coverage_days = max(3, _num(scenario.get('b_target_coverage_days'), 21))
    costs = {
        'A区超期仓储': 0.0,
        '整柜卸货': 0.0,
        '入库操作': 0.0,
        '出库操作': 0.0,
        'A区人员': 0.0,
        '外部送仓运输': 0.0,
        '预约与等待': 0.0,
        '拒收与罚金风险': 0.0,
        'B区面积租金': 0.0,
        'B区人员': 0.0,
        'VFH固定运营': 0.0,
        'VFH平台/操作': 0.0,
        '库内转运': 0.0,
        'VFH建设投入': 0.0,
        '加班': 0.0,
    }
    if mode == 'vfh':
        costs['VFH建设投入'] = _num(scenario.get('vfh_setup_cost'), 85000)

    demand_total = 0.0
    fulfilled_total = 0.0
    transferred_total = initial_b_units
    overtime_hours = 0.0
    utilization_series = []
    forecast = []

    for day in range(horizon):
        progress = day / max(1, horizon - 1)
        growth = (1 + annual_growth) ** (day / 365)
        season = 1 + 0.12 * math.sin((day + 18) / 365 * math.tau)
        noise = max(0.35, rng.gauss(1, density['noise']))
        demand = monthly_demand / 30 * growth * season * noise
        demand_total += demand

        arriving_containers = arrivals.get(day, 0)
        inbound_units = arriving_containers * units_per_container
        if inbound_units:
            a_units += inbound_units
            lots.append({
                'sku': 'MIXED-INBOUND', 'receipt_day': day, 'units': inbound_units,
                'unit_volume_m3': stats['average_volume_m3'], 'source': 'scenario_forecast',
            })
            inbound_pallets = inbound_units / units_per_pallet
            costs['整柜卸货'] += arriving_containers * _num(scenario.get('container_unload_fee'), 420)
            costs['入库操作'] += inbound_pallets * _num(scenario.get('sku_inbound_fee_pallet'), 2.8)

        required = demand + backlog
        used_overtime_pallets = 0.0
        if mode == 'vfh':
            future_daily = monthly_demand / 30 * growth
            target_units = min(
                capacity['capacity_pallets'] * units_per_pallet * target_util,
                future_daily * coverage_days,
            )
            transfer_need = max(0, target_units - b_units)
            requested_pallets = transfer_need / units_per_pallet
            transfer_pallets = min(requested_pallets, regular_capacity + overtime_capacity)
            if transfer_pallets > regular_capacity:
                used_overtime_pallets = transfer_pallets - regular_capacity
            transfer_units = min(a_units, transfer_pallets * units_per_pallet)
            actual_from_lots = _consume_fifo(lots, transfer_units)
            a_units -= actual_from_lots
            b_units += actual_from_lots
            transferred_total += actual_from_lots
            costs['库内转运'] += transfer_pallets * _num(scenario.get('internal_transfer_cost_pallet'), 2.2)
            fulfilled = min(b_units, required)
            b_units -= fulfilled
            costs['VFH平台/操作'] += fulfilled / units_per_pallet * _num(scenario.get('vfh_operating_fee_pallet'), 1.3)
        else:
            outbound_pallets_requested = required / units_per_pallet
            outbound_pallets_capacity = regular_capacity + overtime_capacity
            if outbound_pallets_requested > regular_capacity:
                used_overtime_pallets = min(overtime_capacity, outbound_pallets_requested - regular_capacity)
            fulfilled = min(a_units, outbound_pallets_capacity * units_per_pallet, required)
            actual_from_lots = _consume_fifo(lots, fulfilled)
            a_units -= actual_from_lots
            fulfilled = actual_from_lots

        fulfilled_total += fulfilled
        backlog = max(0, required - fulfilled)
        outbound_pallets = fulfilled / units_per_pallet
        costs['出库操作'] += outbound_pallets * _num(scenario.get('sku_outbound_fee_pallet'), 3.5)
        if used_overtime_pallets:
            overtime_minutes = used_overtime_pallets / max(regular_capacity, 1) * workers * 8 * 60
            daily_overtime_hours = overtime_minutes / 60
            overtime_hours += daily_overtime_hours
            costs['加班'] += daily_overtime_hours * _num(scenario.get('overtime_hourly_cost'), 28)

        if mode == 'without_vfh':
            orders = fulfilled / avg_units_order
            parcel_share = _num(scenario.get('parcel_percent'), 25) / 100
            ltl_share = _num(scenario.get('ltl_percent'), 45) / 100
            ftl_share = _num(scenario.get('ftl_percent'), 30) / 100
            parcel_cost = orders * parcel_share * _num(scenario.get('parcel_cost_order'), 7.8)
            ltl_pallets = outbound_pallets * ltl_share
            ltl_cost = ltl_pallets * _num(scenario.get('ltl_cost_pallet'), 72)
            ftl_loads = outbound_pallets * ftl_share / max(1, _num(scenario.get('ftl_pallets_load'), 26))
            ftl_cost = ftl_loads * _num(scenario.get('ftl_cost_load'), 980)
            delivery_cost = parcel_cost + ltl_cost + ftl_cost
            shipment_events = orders * parcel_share + ltl_pallets + ftl_loads
            costs['外部送仓运输'] += delivery_cost
            costs['预约与等待'] += (ltl_pallets / 8 + ftl_loads) * _num(scenario.get('appointment_fee_load'), 55)
            rejection_rate = _num(scenario.get('rejection_rate_pct'), 3) / 100
            costs['拒收与罚金风险'] += shipment_events * rejection_rate * _num(scenario.get('rejection_penalty_event'), 450)

        costs['A区超期仓储'] += _aged_storage_cost(lots, day, free_days, storage_rate)
        costs['A区人员'] += int(_num(a_settings.get('workers'), 10)) * _num(scenario.get('labor_monthly_cost'), 3200) / 30
        if mode == 'vfh':
            costs['B区面积租金'] += _num(scenario.get('b_area_m2'), 1800) * _num(scenario.get('b_rent_m2_month'), 8.5) / 30
            costs['B区人员'] += workers * _num(scenario.get('labor_monthly_cost'), 3200) / 30
            costs['VFH固定运营'] += _num(scenario.get('vfh_fixed_monthly_cost'), 7500) / 30

        utilization = (
            b_units / (capacity['capacity_pallets'] * units_per_pallet) * 100
            if mode == 'vfh' and capacity['capacity_pallets'] else 0
        )
        utilization_series.append(utilization)
        if day % max(1, horizon // 120) == 0 or day == horizon - 1:
            inbound_daily = inbound_containers * units_per_container / 30
            forecast.append({
                'day': day,
                'date': (date.today() + timedelta(days=day)).isoformat(),
                'at_sea_units': round(inbound_daily * _num(scenario.get('sea_lead_days'), 35) * (1 + progress * annual_growth)),
                'customs_units': round(inbound_daily * _num(scenario.get('customs_lead_days'), 5) * (1 + progress * annual_growth)),
                'a_units': round(a_units),
                'b_units': round(b_units),
                'b_utilization': round(utilization, 1),
                'backlog_units': round(backlog),
            })

    total_cost = sum(costs.values())
    return {
        'mode': mode,
        'costs': {key: _round_money(value) for key, value in costs.items()},
        'total_cost': _round_money(total_cost),
        'annualized_cost': _round_money(total_cost / horizon * 365),
        'service_level': round(fulfilled_total / demand_total * 100, 1) if demand_total else 100,
        'demand_units': round(demand_total),
        'fulfilled_units': round(fulfilled_total),
        'backlog_units': round(backlog),
        'transferred_units': round(transferred_total),
        'overtime_hours': round(overtime_hours, 1),
        'average_b_utilization': round(sum(utilization_series) / len(utilization_series), 1),
        'p95_b_utilization': round(_p95(utilization_series), 1),
        'peak_b_utilization': round(max(utilization_series or [0]), 1),
        'capacity': capacity,
        'forecast': forecast,
    }


def _optimization(scenario, b_settings, stats, without_result):
    monthly_demand = _num(scenario.get('monthly_demand_units'), 12000)
    growth = max(0, _num(scenario.get('business_growth_pct'), 8) / 100)
    peak_daily_units = monthly_demand / 30 * (1 + growth) * 1.30
    upp = max(1, _num(scenario.get('planning_units_per_pallet')) or stats['average_units_per_pallet'])
    coverage = max(3, _num(scenario.get('b_target_coverage_days'), 21))
    target = max(0.35, _num(scenario.get('target_utilization_pct'), 85) / 100)
    current_area = max(250, _num(scenario.get('b_area_m2'), 1800))
    positions_per_m2 = _b_capacity(1000, scenario, b_settings)['capacity_pallets'] / 1000
    required_pallets = peak_daily_units * coverage / upp
    required_area = required_pallets / max(0.01, positions_per_m2 * target)
    low = max(250, math.floor(min(current_area * 0.6, required_area * 0.75) / 250) * 250)
    high = max(low + 500, math.ceil(max(current_area * 1.5, required_area * 1.35) / 250) * 250)
    areas = list(range(int(low), int(high) + 1, 250))[:18]
    daily_pallets = peak_daily_units / upp
    regular_one_worker, _ = _daily_capacity(1, max(1, int(_num(b_settings.get('forklifts'), 2))), scenario, b_settings)
    recommended_workers = max(1, math.ceil(daily_pallets / max(1, regular_one_worker * target)))
    worker_options = range(max(1, recommended_workers - 2), min(30, recommended_workers + 4) + 1)
    service_target = _num(scenario.get('service_target_pct'), 96)
    candidates = []
    annual_without = without_result['annualized_cost']
    for area in areas:
        capacity = _b_capacity(area, scenario, b_settings)
        occupancy = required_pallets / max(1, capacity['capacity_pallets']) * 100
        for workers in worker_options:
            regular, overtime = _daily_capacity(
                workers, max(1, int(_num(b_settings.get('forklifts'), 2))), scenario, b_settings
            )
            service = min(100, (regular + overtime) / max(daily_pallets, 0.01) * 100)
            annual_vfh_cost = (
                area * _num(scenario.get('b_rent_m2_month'), 8.5) * 12
                + workers * _num(scenario.get('labor_monthly_cost'), 3200) * 12
                + _num(scenario.get('vfh_fixed_monthly_cost'), 7500) * 12
                + monthly_demand / upp * 12 * (
                    _num(scenario.get('vfh_operating_fee_pallet'), 1.3)
                    + _num(scenario.get('internal_transfer_cost_pallet'), 2.2)
                )
            )
            feasible = occupancy <= _num(scenario.get('target_utilization_pct'), 85) and service >= service_target
            candidates.append({
                'area_m2': area,
                'workers': workers,
                'capacity_pallets': capacity['capacity_pallets'],
                'peak_utilization': round(occupancy, 1),
                'service_level': round(service, 1),
                'annual_incremental_cost': round(annual_vfh_cost),
                'annual_saving_vs_without': round(annual_without - annual_vfh_cost),
                'feasible': feasible,
            })
    feasible = [item for item in candidates if item['feasible']]
    ranked = sorted(
        feasible or candidates,
        key=lambda item: (
            0 if item['feasible'] else 1,
            item['annual_incremental_cost'],
            abs(item['peak_utilization'] - _num(scenario.get('target_utilization_pct'), 85)),
        ),
    )
    best = ranked[0]
    shortlist = sorted(candidates, key=lambda item: (
        0 if item['feasible'] else 1,
        abs(item['area_m2'] - best['area_m2']) + abs(item['workers'] - best['workers']) * 180,
    ))[:8]
    return best, shortlist


def _sku_policy(stats):
    stocked = sorted(stats['stocked'], key=lambda item: int(item.get('qty') or 0), reverse=True)[:18]
    max_qty = max([int(item.get('qty') or 0) for item in stocked] or [1])
    result = []
    for sku in stocked:
        dims = [_num(sku.get(key)) for key in ('length_cm', 'width_cm', 'height_cm')]
        complete = all(value > 0 for value in dims)
        max_dim = max(dims or [0])
        weight = _num(sku.get('weight_kg'))
        units_per_pallet = max(1, int(sku.get('units_per_pallet') or 1))
        velocity = int(sku.get('qty') or 0) / max_qty
        if not complete:
            placement, reason = '待补数据', '缺少完整尺寸，暂不自动决策'
        elif max_dim >= 120 or weight >= 45 or units_per_pallet <= 2:
            placement, reason = '地堆', '大件/重货或单托件数较低'
        elif velocity >= 0.55 and max_dim < 75:
            placement, reason = '货架近拣选口', '高周转且体积适合货架'
        else:
            placement, reason = '货架', '尺寸与周转适合标准货位'
        result.append({
            'code': sku.get('code'), 'name': sku.get('name'), 'category': sku.get('category'),
            'qty': int(sku.get('qty') or 0), 'dimensions_cm': dims,
            'weight_kg': weight, 'units_per_pallet': units_per_pallet,
            'placement': placement, 'reason': reason,
        })
    return result


def _batch_preview(stats, scenario, vfh_result, batches=None):
    today = date.today()
    if batches:
        labels = {
            'A': ('A / 货主库存', '货主企业'),
            'B': ('B / 平台前置仓', '平台方'),
            'SEA': ('海运在途', '货主企业'),
            'CUSTOMS': ('虚拟海关', '货主企业'),
        }
        rows = []
        for batch in sorted(batches, key=lambda item: (item.get('receipt_date', ''), item.get('batch_id', '')))[:50]:
            try:
                receipt = date.fromisoformat(str(batch.get('receipt_date')))
                age = max(0, (today - receipt).days)
            except ValueError:
                receipt = today
                age = 0
            zone_code = str(batch.get('zone') or 'A').upper()
            zone, owner = labels.get(zone_code, (zone_code, '待确认'))
            qty = int(batch.get('qty') or 0)
            rows.append({
                'batch_id': batch.get('batch_id'),
                'sku': batch.get('sku_code'),
                'receipt_date': receipt.isoformat(),
                'age_days': age,
                'qty': qty,
                'volume_m3': round(qty * _num(batch.get('unit_volume_m3')), 2) if batch.get('unit_volume_m3') else None,
                'zone': zone,
                'title_owner': owner,
                'fee_status': 'A区计费' if zone_code == 'A' and age > int(_num(scenario.get('a_free_days'), 60)) else '未触发A区超期费',
                'source': '用户导入批次数据',
            })
        return rows
    avg_age = int(_num(scenario.get('current_avg_age_days'), 42))
    rows = []
    for index, sku in enumerate(sorted(stats['stocked'], key=lambda item: int(item.get('qty') or 0), reverse=True)[:16]):
        age = max(0, avg_age + [-24, -8, 8, 24][index % 4])
        b_share = 0.32 if scenario.get('scenario_mode') != 'without_vfh' else 0
        zone = 'B / 平台前置仓' if index % 3 == 0 and b_share else 'A / 货主库存'
        owner = '平台方' if zone.startswith('B') else '货主企业'
        qty = int(sku.get('qty') or 0)
        rows.append({
            'batch_id': f'SCE-{index + 1:03d}',
            'sku': sku.get('code'),
            'receipt_date': (today - timedelta(days=age)).isoformat(),
            'age_days': age,
            'qty': qty,
            'volume_m3': round(qty * (_num(sku.get('volume_m3')) or stats['average_volume_m3']), 2),
            'zone': zone,
            'title_owner': owner,
            'fee_status': 'A区计费' if zone.startswith('A') and age > int(_num(scenario.get('a_free_days'), 60)) else '未触发A区超期费',
            'source': 'SKU数量来自库存；入库日期为情景假设',
        })
    return rows


def analyze_vfh(scenario, a_settings, b_settings, skus, seed=42, batches=None):
    stats = _sku_stats(skus)
    batches = batches or []
    without = _simulate('without_vfh', scenario, a_settings, b_settings, stats, seed, batches)
    with_vfh = _simulate('vfh', scenario, a_settings, b_settings, stats, seed, batches)
    horizon = int(_num(scenario.get('horizon_days'), 365))
    savings = without['total_cost'] - with_vfh['total_cost']
    setup = _num(scenario.get('vfh_setup_cost'), 85000)
    recurring_with = with_vfh['total_cost'] - setup
    monthly_recurring_saving = (without['total_cost'] - recurring_with) / horizon * 30
    payback_months = setup / monthly_recurring_saving if monthly_recurring_saving > 0 else None
    best, shortlist = _optimization(scenario, b_settings, stats, without)

    current_area = _num(scenario.get('b_area_m2'), 1800)
    mode = scenario.get('scenario_mode', 'planning')
    if mode == 'planning':
        if savings > 0 and (payback_months is None or payback_months <= 36):
            recommendation = '建议建立VFH-HUB'
            direction = 'establish'
        else:
            recommendation = '暂不建议按当前参数建立VFH'
            direction = 'wait'
    elif best['area_m2'] > current_area * 1.10 or with_vfh['p95_b_utilization'] > _num(scenario.get('target_utilization_pct'), 85):
        recommendation = '建议扩充B区面积或增加货位'
        direction = 'expand'
    elif best['area_m2'] < current_area * 0.78 and with_vfh['average_b_utilization'] < 55:
        recommendation = '建议缩减B区或调整货架/地堆结构'
        direction = 'reduce'
    else:
        recommendation = '建议维持B区并持续监测'
        direction = 'hold'

    density = DATA_DENSITY.get(scenario.get('data_density'), DATA_DENSITY['warning'])
    capacity = _b_capacity(best['area_m2'], scenario, b_settings)
    upp = max(1, _num(scenario.get('planning_units_per_pallet')) or stats['average_units_per_pallet'])
    area_throughput = capacity['capacity_pallets'] * upp / max(3, _num(scenario.get('b_target_coverage_days'), 21)) * 30
    regular, overtime = _daily_capacity(best['workers'], max(1, int(_num(b_settings.get('forklifts'), 2))), scenario, b_settings)
    labor_throughput = (regular + overtime) * upp * 30
    supported_monthly_units = min(area_throughput, labor_throughput)

    warnings = []
    if scenario.get('data_density') != 'detailed':
        warnings.append('当前结果主要用于方向判断；导入批次级入库、出库和账单数据后可收窄区间。')
    if batches:
        warnings.append(f'已使用 {len(batches)} 条导入批次作为当前库存与A区库龄基线；未来需求和费率仍为情景假设。')
    else:
        warnings.append('现有数据库没有批次入库日期，A区库龄按可编辑的平均库龄和分布假设计算。')
    if any(_num(sku.get('length_cm')) <= 0 for sku in stats['stocked']):
        warnings.append('部分SKU缺少尺寸，库容折算使用已知SKU平均体积；相关SKU已标记待补数据。')

    actions = [
        f"目标B区：约 {best['area_m2']:,} ㎡、{best['workers']} 名作业人员，峰值利用率约 {best['peak_utilization']}%。",
        f"按当前情景，B区可支持约 {supported_monthly_units:,.0f} 件/月；相对输入业务量的余量约 {supported_monthly_units - _num(scenario.get('monthly_demand_units'), 12000):,.0f} 件/月。",
    ]
    if with_vfh['overtime_hours'] > without['overtime_hours'] * 1.15:
        actions.append('B区加班压力较高，优先比较增加1名员工与持续加班的全年成本。')
    if with_vfh['p95_b_utilization'] > _num(scenario.get('target_utilization_pct'), 85):
        actions.append('B区P95利用率超过目标，需增加面积/货位或缩短A→B补货覆盖天数。')

    zone_totals = {zone: 0 for zone in ('SEA', 'CUSTOMS', 'A', 'B')}
    for batch in batches:
        zone = str(batch.get('zone') or '').upper()
        if zone in zone_totals:
            zone_totals[zone] += int(batch.get('qty') or 0)

    return {
        'decision': {
            'recommendation': recommendation,
            'direction': direction,
            'horizon_days': horizon,
            'currency': scenario.get('currency', 'EUR'),
            'savings': _round_money(savings),
            'annualized_savings': _round_money(savings / horizon * 365),
            'payback_months': round(payback_months, 1) if payback_months else None,
            'recommended_area_m2': best['area_m2'],
            'recommended_workers': best['workers'],
            'supported_monthly_units': round(supported_monthly_units),
            'business_headroom_units_month': round(supported_monthly_units - _num(scenario.get('monthly_demand_units'), 12000)),
            'data_density': scenario.get('data_density', 'warning'),
            'data_density_label': density['label'],
            'scenario_band_pct': density['band'],
        },
        'without_vfh': without,
        'with_vfh': with_vfh,
        'recommended_capacity': capacity,
        'shortlist': shortlist,
        'sku_policy': _sku_policy(stats),
        'batch_preview': _batch_preview(stats, scenario, with_vfh, batches),
        'current_inventory': {
            'has_imported_batches': bool(batches),
            'batch_count': len(batches),
            'sea_units': zone_totals['SEA'],
            'customs_units': zone_totals['CUSTOMS'],
            'a_units': zone_totals['A'] if batches else stats['total_units'],
            'b_units': zone_totals['B'],
        },
        'actions': actions,
        'warnings': warnings,
        'source_summary': {
            'observed': ['SKU编码、品名、品类、现有库存数量、已填写的尺寸和打托规格'] + (['导入的批次号、入库日期、数量和所在区域'] if batches else []),
            'assumed': (['未来业务量、各项费率、运输方式比例、海运/清关时效'] if batches else ['批次入库日期、未来业务量、各项费率、运输方式比例、海运/清关时效']),
            'stock_units': stats['total_units'],
            'stock_pallets': stats['stock_pallets'],
            'known_stock_volume_m3': round(stats['total_volume_m3'], 2),
        },
    }
