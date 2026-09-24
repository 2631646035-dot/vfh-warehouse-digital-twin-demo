# -*- coding: utf-8 -*-
"""Portfolio-safe Warehouse Studio API。"""
import os
import sys
from datetime import date
from typing import Literal

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from data_loader import (
    get_available_dates,
    get_inventory_batches,
    get_model_settings,
    get_sku,
    get_sku_catalog,
    get_vfh_scenario,
    load_stock_data,
    load_summary,
    load_warehouse_config,
    save_model_settings,
    replace_inventory_batches,
    save_sku,
    save_vfh_scenario,
)
from simulation_engine import run_simulation
from vfh_engine import analyze_vfh

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = FastAPI(title='Warehouse Digital Twin Demo', version='2.0.0')


class SettingsPayload(BaseModel):
    warehouse_width_m: float = Field(ge=10, le=250)
    warehouse_depth_m: float = Field(ge=10, le=250)
    shelf_width_m: float = Field(ge=1, le=6)
    shelf_depth_m: float = Field(ge=0.5, le=3)
    shelf_height_m: float = Field(ge=0.5, le=4)
    shelf_levels: int = Field(ge=1, le=10)
    aisle_percent: float = Field(ge=10, le=60)
    floor_percent: float = Field(ge=0, le=70)
    workers: int = Field(ge=1, le=200)
    labor_efficiency: float = Field(ge=20, le=150)
    forklifts: int = Field(ge=1, le=50)
    forklift_efficiency: float = Field(ge=20, le=150)
    inbound_pallets_day: int = Field(ge=0, le=10000)
    outbound_orders_day: int = Field(ge=0, le=50000)
    avg_lines_order: float = Field(ge=1, le=50)


class SkuPayload(BaseModel):
    name: str = Field(max_length=200)
    category: str = Field(max_length=100)
    length_cm: float = Field(ge=0.1, le=1000)
    width_cm: float = Field(ge=0.1, le=1000)
    height_cm: float = Field(ge=0.1, le=1000)
    weight_kg: float = Field(ge=0, le=10000)
    storage_type: Literal['floor_only', 'flexible', 'shelf_only']
    pallet_length_cm: float = Field(ge=40, le=300)
    pallet_width_cm: float = Field(ge=40, le=300)
    max_stack_height_cm: float = Field(ge=30, le=500)
    units_per_layer: int = Field(ge=1, le=1000)
    layers_per_pallet: int = Field(ge=1, le=100)
    units_per_pallet: int = Field(ge=1, le=100000)
    rotation_allowed: bool = True


class SimulationPayload(BaseModel):
    zone: Literal['A', 'B'] = 'A'
    seed: int = Field(default=42, ge=0, le=1_000_000)
    settings: SettingsPayload | None = None


class VfhScenarioPayload(BaseModel):
    country: str = Field(default='Demo-Europe', min_length=1, max_length=100)
    currency: str = Field(default='EUR', min_length=3, max_length=8)
    scenario_mode: Literal['planning', 'existing', 'without_vfh'] = 'planning'
    data_density: Literal['detailed', 'warning', 'sparse'] = 'warning'
    horizon_days: int = Field(default=365, ge=90, le=1095)
    business_growth_pct: float = Field(default=8, ge=-80, le=300)
    monthly_demand_units: float = Field(default=12000, ge=0, le=100_000_000)
    avg_units_order: float = Field(default=2.2, ge=0.1, le=1000)
    planning_units_per_pallet: float = Field(default=0, ge=0, le=100000)
    current_avg_age_days: int = Field(default=42, ge=0, le=3650)
    inbound_containers_month: float = Field(default=4, ge=0, le=10000)
    units_per_container: float = Field(default=3200, ge=0, le=1_000_000)
    sea_lead_days: float = Field(default=35, ge=0, le=365)
    customs_lead_days: float = Field(default=5, ge=0, le=180)
    a_free_days: int = Field(default=60, ge=0, le=3650)
    a_storage_rate_m3_day: float = Field(default=0.022, ge=0, le=1000)
    container_unload_fee: float = Field(default=420, ge=0, le=1_000_000)
    sku_inbound_fee_pallet: float = Field(default=2.8, ge=0, le=100000)
    sku_outbound_fee_pallet: float = Field(default=3.5, ge=0, le=100000)
    b_area_m2: float = Field(default=1800, ge=50, le=1_000_000)
    b_rent_m2_month: float = Field(default=8.5, ge=0, le=100000)
    b_workers: int = Field(default=3, ge=1, le=1000)
    labor_monthly_cost: float = Field(default=3200, ge=0, le=1_000_000)
    overtime_hourly_cost: float = Field(default=28, ge=0, le=100000)
    overtime_limit_hours_day: float = Field(default=2, ge=0, le=16)
    vfh_setup_cost: float = Field(default=85000, ge=0, le=1_000_000_000)
    vfh_fixed_monthly_cost: float = Field(default=7500, ge=0, le=100_000_000)
    vfh_operating_fee_pallet: float = Field(default=1.3, ge=0, le=100000)
    internal_transfer_cost_pallet: float = Field(default=2.2, ge=0, le=100000)
    minutes_per_internal_pallet: float = Field(default=11, ge=0.1, le=1000)
    b_target_coverage_days: float = Field(default=21, ge=1, le=365)
    b_initial_utilization_pct: float = Field(default=52, ge=0, le=100)
    target_utilization_pct: float = Field(default=85, ge=20, le=99)
    service_target_pct: float = Field(default=96, ge=20, le=100)
    office_percent: float = Field(default=5, ge=0, le=50)
    rest_percent: float = Field(default=5, ge=0, le=50)
    parcel_percent: float = Field(default=25, ge=0, le=100)
    ltl_percent: float = Field(default=45, ge=0, le=100)
    ftl_percent: float = Field(default=30, ge=0, le=100)
    parcel_cost_order: float = Field(default=7.8, ge=0, le=100000)
    ltl_cost_pallet: float = Field(default=72, ge=0, le=100000)
    ftl_cost_load: float = Field(default=980, ge=0, le=1_000_000)
    ftl_pallets_load: float = Field(default=26, ge=1, le=1000)
    appointment_fee_load: float = Field(default=55, ge=0, le=100000)
    rejection_rate_pct: float = Field(default=3, ge=0, le=100)
    rejection_penalty_event: float = Field(default=450, ge=0, le=1_000_000)


class VfhRunPayload(BaseModel):
    scenario: VfhScenarioPayload | None = None
    seed: int = Field(default=42, ge=0, le=1_000_000)


class InventoryBatchPayload(BaseModel):
    batch_id: str = Field(min_length=1, max_length=100)
    sku_code: str = Field(min_length=1, max_length=100)
    receipt_date: str = Field(min_length=10, max_length=10)
    qty: int = Field(gt=0, le=100_000_000)
    zone: Literal['A', 'B', 'SEA', 'CUSTOMS'] = 'A'
    status: Literal['active', 'closed'] = 'active'
    unit_volume_m3: float | None = Field(default=None, gt=0, le=1000)


class BatchImportPayload(BaseModel):
    country: str = Field(default='Demo-Europe', min_length=1, max_length=100)
    rows: list[InventoryBatchPayload] = Field(min_length=1, max_length=100_000)


@app.get('/api/warehouse')
def api_warehouse():
    return load_warehouse_config()


@app.get('/api/stocks')
def api_stocks(date: str = Query(default=None, description='日期 YYYY-MM-DD，默认最新')):
    return load_stock_data(date)


@app.get('/api/summary')
def api_summary(date: str = Query(default=None, description='日期 YYYY-MM-DD，默认最新')):
    return load_summary(date)


@app.get('/api/dates')
def api_dates():
    return {'dates': get_available_dates()}


@app.get('/api/model-settings')
def api_model_settings():
    return get_model_settings()


@app.put('/api/model-settings/{zone}')
def api_save_model_settings(zone: Literal['A', 'B'], payload: SettingsPayload):
    values = payload.model_dump()
    if values['aisle_percent'] + values['floor_percent'] > 82:
        raise HTTPException(400, '过道与地堆面积之和不能超过82%')
    return save_model_settings(zone, values)


@app.get('/api/skus')
def api_skus(q: str = '', limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0)):
    return get_sku_catalog(q, limit, offset)


@app.get('/api/skus/{code}')
def api_sku(code: str):
    sku = get_sku(code)
    if not sku:
        raise HTTPException(404, 'SKU不存在')
    return sku


@app.put('/api/skus/{code}')
def api_save_sku(code: str, payload: SkuPayload):
    sku = save_sku(code, payload.model_dump())
    if not sku:
        raise HTTPException(404, 'SKU不存在')
    return sku


@app.post('/api/simulation/run')
def api_run_simulation(payload: SimulationPayload):
    settings = payload.settings.model_dump() if payload.settings else get_model_settings(payload.zone)
    if settings['aisle_percent'] + settings['floor_percent'] > 82:
        raise HTTPException(400, '过道与地堆面积之和不能超过82%')
    catalog = get_sku_catalog('', 500, 0)['items']
    zone_stocks = {stock['code']: stock for stock in load_stock_data() if stock['zone'] == payload.zone}
    skus = []
    for sku in catalog:
        merged = dict(sku)
        merged['qty'] = zone_stocks.get(sku['code'], {}).get('qty', 0)
        skus.append(merged)
    return run_simulation(settings, skus, payload.seed)


def _validate_vfh(values):
    transport_total = values['parcel_percent'] + values['ltl_percent'] + values['ftl_percent']
    if abs(transport_total - 100) > 0.1:
        raise HTTPException(400, '快递、散托和整车比例之和必须为100%')
    if values['office_percent'] + values['rest_percent'] >= 50:
        raise HTTPException(400, '办公区与休息区合计占比必须小于50%')


@app.get('/api/vfh/scenario')
def api_vfh_scenario(country: str = 'Demo-Europe'):
    return get_vfh_scenario(country)


@app.put('/api/vfh/scenario')
def api_save_vfh_scenario(payload: VfhScenarioPayload):
    values = payload.model_dump()
    _validate_vfh(values)
    return save_vfh_scenario(payload.country, values)


@app.get('/api/vfh/batches')
def api_vfh_batches(country: str = 'Demo-Europe'):
    rows = get_inventory_batches(country)
    return {'country': country, 'count': len(rows), 'rows': rows}


@app.post('/api/vfh/batches/import')
def api_import_vfh_batches(payload: BatchImportPayload):
    rows = [row.model_dump() for row in payload.rows]
    batch_ids = [row['batch_id'] for row in rows]
    if len(batch_ids) != len(set(batch_ids)):
        raise HTTPException(400, '批次号必须唯一')
    for row in rows:
        try:
            date.fromisoformat(row['receipt_date'])
        except ValueError as exc:
            raise HTTPException(400, f"批次 {row['batch_id']} 的入库日期无效，应为 YYYY-MM-DD") from exc
    known_skus = {item['code'] for item in get_sku_catalog('', 500, 0)['items']}
    unknown_skus = sorted({row['sku_code'] for row in rows if row['sku_code'] not in known_skus})
    saved = replace_inventory_batches(payload.country, rows)
    return {
        'country': payload.country,
        'count': len(saved),
        'unknown_skus': unknown_skus,
        'rows': saved,
    }


@app.post('/api/vfh/analyze')
def api_analyze_vfh(payload: VfhRunPayload):
    scenario = payload.scenario.model_dump() if payload.scenario else get_vfh_scenario('Demo-Europe')
    _validate_vfh(scenario)
    catalog = get_sku_catalog('', 500, 0)['items']
    batches = get_inventory_batches(scenario.get('country', 'Demo-Europe'))
    imported_qty = {}
    for batch in batches:
        if batch['zone'] in ('A', 'B'):
            imported_qty[batch['sku_code']] = imported_qty.get(batch['sku_code'], 0) + batch['qty']
    a_stock = {stock['code']: stock for stock in load_stock_data() if stock['zone'] == 'A'}
    skus = []
    for sku in catalog:
        merged = dict(sku)
        merged['qty'] = imported_qty.get(sku['code'], a_stock.get(sku['code'], {}).get('qty', 0))
        skus.append(merged)
    return analyze_vfh(
        scenario,
        get_model_settings('A'),
        get_model_settings('B'),
        skus,
        payload.seed,
        batches,
    )


@app.get('/', response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))


app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
