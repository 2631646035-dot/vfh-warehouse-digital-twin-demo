# -*- coding: utf-8 -*-
"""轻量仓内离散事件仿真，用于方案对比与3D任务回放。"""
from __future__ import annotations

import math
import random
import statistics


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _next_arrivals(rng, count, horizon):
    if count <= 0:
        return []
    rate = count / horizon
    now = 0.0
    arrivals = []
    while now < horizon and len(arrivals) < count * 2:
        now += rng.expovariate(rate)
        if now < horizon:
            arrivals.append(now)
    return arrivals


def calculate_capacity(settings, skus):
    area = settings['warehouse_width_m'] * settings['warehouse_depth_m']
    floor_area = area * settings['floor_percent'] / 100
    aisle_area = area * settings['aisle_percent'] / 100
    rack_area = max(0, area - floor_area - aisle_area)
    rack_footprint = settings['shelf_width_m'] * settings['shelf_depth_m'] * 1.18
    rack_bays = max(0, math.floor(rack_area / max(rack_footprint, 0.1)))
    rack_positions = rack_bays * settings['shelf_levels']
    floor_positions = max(0, math.floor(floor_area / (1.2 * 0.8 * 1.24)))
    capacity_pallets = rack_positions + floor_positions
    stock_pallets = 0
    for sku in skus:
        units = max(int(sku.get('units_per_pallet') or 1), 1)
        stock_pallets += math.ceil(int(sku.get('qty') or 0) / units)
    utilization = (stock_pallets / capacity_pallets * 100) if capacity_pallets else 0
    return {
        'warehouse_area_m2': round(area, 1),
        'rack_area_m2': round(rack_area, 1),
        'floor_area_m2': round(floor_area, 1),
        'aisle_area_m2': round(aisle_area, 1),
        'rack_bays': rack_bays,
        'rack_positions': rack_positions,
        'floor_positions': floor_positions,
        'capacity_pallets': capacity_pallets,
        'stock_pallets': stock_pallets,
        'space_utilization': round(utilization, 1),
    }


def run_simulation(settings, skus, seed=42):
    rng = random.Random(seed)
    horizon = 480.0
    inbound = int(settings['inbound_pallets_day'])
    outbound_lines = int(settings['outbound_orders_day'] * settings['avg_lines_order'])
    tasks = [(t, 'inbound') for t in _next_arrivals(rng, inbound, horizon)]
    tasks += [(t, 'outbound') for t in _next_arrivals(rng, outbound_lines, horizon)]
    tasks.sort(key=lambda item: item[0])

    workers = [0.0] * max(1, int(settings['workers']))
    forklifts = [0.0] * max(1, int(settings['forklifts']))
    labor_factor = max(settings['labor_efficiency'] / 100, 0.1)
    forklift_factor = max(settings['forklift_efficiency'] / 100, 0.1)
    aisle_factor = 1 + max(0, 28 - settings['aisle_percent']) / 42
    diagonal = math.hypot(settings['warehouse_width_m'], settings['warehouse_depth_m'])

    completed = 0
    backlog = 0
    waits = []
    cycles = []
    worker_busy = 0.0
    forklift_busy = 0.0
    events = []
    sku_pool = [sku for sku in skus if int(sku.get('qty') or 0) > 0] or skus

    for arrival, kind in tasks:
        worker_idx = min(range(len(workers)), key=workers.__getitem__)
        needs_forklift = kind == 'inbound' or rng.random() < 0.48
        forklift_idx = min(range(len(forklifts)), key=forklifts.__getitem__) if needs_forklift else None
        start = max(arrival, workers[worker_idx])
        if forklift_idx is not None:
            start = max(start, forklifts[forklift_idx])

        base = 9.0 if kind == 'inbound' else 4.5
        variation = rng.uniform(0.82, 1.22)
        travel = (diagonal / 48) * aisle_factor / forklift_factor if needs_forklift else 0.8
        service = (base * variation / labor_factor) + travel
        finish = start + service
        wait = max(0, start - arrival)

        workers[worker_idx] = finish
        worker_busy += service
        if forklift_idx is not None:
            forklifts[forklift_idx] = finish
            forklift_busy += travel + base * 0.35

        waits.append(wait)
        cycles.append(finish - arrival)
        if finish <= horizon:
            completed += 1
        else:
            backlog += 1

        if len(events) < 180:
            sku = rng.choice(sku_pool) if sku_pool else {'code': 'EMPTY'}
            events.append({
                'time': round(start, 2), 'finish': round(finish, 2), 'kind': kind,
                'resource': f"FL-{forklift_idx + 1:02d}" if forklift_idx is not None else f"WK-{worker_idx + 1:02d}",
                'sku': sku.get('code', 'UNKNOWN'),
                'from': 'dock' if kind == 'inbound' else 'rack',
                'to': 'rack' if kind == 'inbound' else 'dispatch',
            })

    capacity = calculate_capacity(settings, skus)
    worker_util = min(100, worker_busy / (len(workers) * horizon) * 100)
    forklift_util = min(100, forklift_busy / (len(forklifts) * horizon) * 100)
    avg_cycle = statistics.fmean(cycles) if cycles else 0
    avg_wait = statistics.fmean(waits) if waits else 0
    risk = '正常'
    if backlog > max(8, len(tasks) * 0.08) or max(worker_util, forklift_util) > 92:
        risk = '高负荷'
    elif backlog or max(worker_util, forklift_util) > 80:
        risk = '需关注'
    if capacity['space_utilization'] > 95:
        risk = '库容风险'

    return {
        'capacity': capacity,
        'operations': {
            'generated_tasks': len(tasks), 'completed_tasks': completed,
            'backlog_tasks': backlog,
            'completion_rate': round(completed / len(tasks) * 100, 1) if tasks else 100,
            'average_wait_min': round(avg_wait, 1), 'average_cycle_min': round(avg_cycle, 1),
            'p95_cycle_min': round(_percentile(cycles, 0.95), 1),
            'worker_utilization': round(worker_util, 1),
            'forklift_utilization': round(forklift_util, 1), 'risk': risk,
        },
        'events': events,
        'seed': seed,
        'horizon_minutes': int(horizon),
    }
