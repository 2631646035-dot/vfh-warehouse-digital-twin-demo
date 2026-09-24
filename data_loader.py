# -*- coding: utf-8 -*-
"""数据加载模块 — 从 SQLite 读取仓库、库存、SKU和可编辑方案参数。"""
import json
import math
import os
import sqlite3
from datetime import date, datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), 'warehouse.db')


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


DEFAULT_MODEL_SETTINGS = {
    "A": {
        "zone": "A", "warehouse_width_m": 60, "warehouse_depth_m": 72,
        "shelf_width_m": 2.7, "shelf_depth_m": 1.1, "shelf_height_m": 1.6,
        "shelf_levels": 4, "aisle_percent": 30, "floor_percent": 18,
        "workers": 12, "labor_efficiency": 86, "forklifts": 3,
        "forklift_efficiency": 82, "inbound_pallets_day": 72,
        "outbound_orders_day": 420, "avg_lines_order": 2.4,
    },
    "B": {
        "zone": "B", "warehouse_width_m": 55, "warehouse_depth_m": 64,
        "shelf_width_m": 2.7, "shelf_depth_m": 1.1, "shelf_height_m": 1.6,
        "shelf_levels": 4, "aisle_percent": 32, "floor_percent": 34,
        "workers": 8, "labor_efficiency": 80, "forklifts": 2,
        "forklift_efficiency": 78, "inbound_pallets_day": 48,
        "outbound_orders_day": 260, "avg_lines_order": 2.1,
    },
}


DEFAULT_VFH_SCENARIO = {
    'country': 'Demo-Europe', 'currency': 'EUR', 'scenario_mode': 'planning',
    'data_density': 'warning', 'horizon_days': 365, 'business_growth_pct': 8,
    'monthly_demand_units': 12000, 'avg_units_order': 2.2,
    'planning_units_per_pallet': 0, 'current_avg_age_days': 42,
    'inbound_containers_month': 4, 'units_per_container': 3200,
    'sea_lead_days': 35, 'customs_lead_days': 5,
    'a_free_days': 60, 'a_storage_rate_m3_day': 0.022,
    'container_unload_fee': 420, 'sku_inbound_fee_pallet': 2.8,
    'sku_outbound_fee_pallet': 3.5,
    'b_area_m2': 1800, 'b_rent_m2_month': 8.5,
    'b_workers': 3, 'labor_monthly_cost': 3200,
    'overtime_hourly_cost': 28, 'overtime_limit_hours_day': 2,
    'vfh_setup_cost': 85000, 'vfh_fixed_monthly_cost': 7500,
    'vfh_operating_fee_pallet': 1.3, 'internal_transfer_cost_pallet': 2.2,
    'minutes_per_internal_pallet': 11, 'b_target_coverage_days': 21,
    'b_initial_utilization_pct': 52, 'target_utilization_pct': 85,
    'service_target_pct': 96, 'office_percent': 5, 'rest_percent': 5,
    'parcel_percent': 25, 'ltl_percent': 45, 'ftl_percent': 30,
    'parcel_cost_order': 7.8, 'ltl_cost_pallet': 72,
    'ftl_cost_load': 980, 'ftl_pallets_load': 26,
    'appointment_fee_load': 55, 'rejection_rate_pct': 3,
    'rejection_penalty_event': 450,
}


def ensure_runtime_schema():
    """为优化版补充可编辑配置；对原有数据库是幂等迁移。"""
    conn = _get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS model_settings (
            zone TEXT PRIMARY KEY CHECK(zone IN ('A','B')),
            warehouse_width_m REAL NOT NULL,
            warehouse_depth_m REAL NOT NULL,
            shelf_width_m REAL NOT NULL,
            shelf_depth_m REAL NOT NULL,
            shelf_height_m REAL NOT NULL,
            shelf_levels INTEGER NOT NULL,
            aisle_percent REAL NOT NULL,
            floor_percent REAL NOT NULL,
            workers INTEGER NOT NULL,
            labor_efficiency REAL NOT NULL,
            forklifts INTEGER NOT NULL,
            forklift_efficiency REAL NOT NULL,
            inbound_pallets_day INTEGER NOT NULL,
            outbound_orders_day INTEGER NOT NULL,
            avg_lines_order REAL NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sku_pallet_specs (
            sku_code TEXT PRIMARY KEY,
            pallet_length_cm REAL NOT NULL DEFAULT 120,
            pallet_width_cm REAL NOT NULL DEFAULT 80,
            max_stack_height_cm REAL NOT NULL DEFAULT 180,
            units_per_layer INTEGER NOT NULL DEFAULT 1,
            layers_per_pallet INTEGER NOT NULL DEFAULT 1,
            units_per_pallet INTEGER NOT NULL DEFAULT 1,
            rotation_allowed INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (sku_code) REFERENCES sku_catalog(code)
        );

        CREATE TABLE IF NOT EXISTS vfh_scenarios (
            country TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inventory_batches (
            country TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            sku_code TEXT NOT NULL,
            receipt_date TEXT NOT NULL,
            qty INTEGER NOT NULL,
            zone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            unit_volume_m3 REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (country, batch_id)
        );
    ''')
    now = datetime.now(timezone.utc).isoformat()
    for zone, settings in DEFAULT_MODEL_SETTINGS.items():
        conn.execute('''
            INSERT OR IGNORE INTO model_settings VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            zone, settings['warehouse_width_m'], settings['warehouse_depth_m'],
            settings['shelf_width_m'], settings['shelf_depth_m'], settings['shelf_height_m'],
            settings['shelf_levels'], settings['aisle_percent'], settings['floor_percent'],
            settings['workers'], settings['labor_efficiency'], settings['forklifts'],
            settings['forklift_efficiency'], settings['inbound_pallets_day'],
            settings['outbound_orders_day'], settings['avg_lines_order'], now,
        ))
    conn.execute(
        'INSERT OR IGNORE INTO vfh_scenarios(country,config_json,updated_at) VALUES (?,?,?)',
        ('Demo-Europe', json.dumps(DEFAULT_VFH_SCENARIO, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()


def load_warehouse_config():
    config = {
        "zone_a": {"name": "A区 (货主自营仓)", "width": 60, "depth": 72,
                   "shelf_rows": 20, "shelf_cols": 100, "shelf_used": 1600,
                   "floor_area": 2000, "floor_pallets": 0},
        "zone_b": {"name": "B区 (VFH)", "width": 55, "depth": 64,
                   "shelf_rows": 15, "shelf_cols": 100, "shelf_used": 138,
                   "floor_area": 600, "floor_pallets": 2390},
        "pallet": {"width": 1.2, "depth": 0.8, "height": 0.15},
        "shelf_slot": {"width": 1.3, "depth": 1.0, "height": 0.9},
    }
    if not os.path.exists(DB_PATH):
        return config

    try:
        conn = _get_conn()
        rows = conn.execute("SELECT zone, shelf_total_slots, shelf_used, floor_area, floor_pallets, width_m, depth_m, label FROM warehouse_params").fetchall()
        for r in rows:
            key = "zone_a" if r['zone'] == 'A' else 'zone_b'
            if r['shelf_total_slots']:
                config[key]['shelf_rows'] = min(r['shelf_total_slots'] // 10, 25) or 20
                config[key]['shelf_cols'] = max(r['shelf_total_slots'] // config[key]['shelf_rows'], 10)
            if r['shelf_used']: config[key]['shelf_used'] = r['shelf_used']
            if r['floor_area']: config[key]['floor_area'] = r['floor_area']
            if r['floor_pallets']: config[key]['floor_pallets'] = r['floor_pallets']
            if r['width_m']: config[key]['width'] = r['width_m']
            if r['depth_m']: config[key]['depth'] = r['depth_m']
        conn.close()
    except Exception:
        pass
    return config


def load_stock_data(date_str=None):
    if not os.path.exists(DB_PATH):
        return _legacy_load()

    conn = _get_conn()

    if date_str is None:
        date_str = conn.execute("SELECT MAX(date) FROM daily_stock").fetchone()[0]
        if not date_str:
            conn.close()
            return []

    rows = conn.execute('''
        SELECT ds.sku_code, ds.zone, ds.qty, ds.storage_type,
               c.name, c.category, c.length_cm, c.width_cm, c.height_cm, c.weight_kg, c.volume_m3
        FROM daily_stock ds
        LEFT JOIN sku_catalog c ON c.code = ds.sku_code
        WHERE ds.date = ?
        ORDER BY ds.qty DESC
    ''', (date_str,)).fetchall()

    stocks = []
    for r in rows:
        stocks.append({
            'code': r['sku_code'], 'name': r['name'] or '', 'category': r['category'] or '',
            'length': r['length_cm'] or 0, 'width': r['width_cm'] or 0,
            'height': r['height_cm'] or 0, 'weight': r['weight_kg'] or 0,
            'volume': round(r['volume_m3'] or 0, 4), 'qty': r['qty'] or 0,
            'zone': r['zone'] or 'A',
            'storage': 'floor' if r['storage_type'] == 'floor_only' else 'flex',
        })
    conn.close()
    return stocks


def load_summary(date_str=None):
    stocks = load_stock_data(date_str)
    by_cat = {}
    for s in stocks:
        c = s['category'] or '其他'
        if c not in by_cat:
            by_cat[c] = {'total': 0, 'floor': 0, 'flex': 0, 'count': 0}
        by_cat[c]['total'] += s['qty']
        by_cat[c]['count'] += 1
        if s['storage'] == 'floor':
            by_cat[c]['floor'] += s['qty']
        else:
            by_cat[c]['flex'] += s['qty']

    total_floor = sum(d['floor'] for d in by_cat.values())
    total_flex = sum(d['flex'] for d in by_cat.values())

    return {
        'total_skus': len(stocks),
        'total_units': total_floor + total_flex,
        'floor_only': total_floor,
        'flexible': total_flex,
        'by_category': by_cat,
        'date': date_str or 'latest',
    }


def get_available_dates():
    """返回所有有数据的历史日期"""
    if not os.path.exists(DB_PATH):
        return []
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT date FROM daily_stock ORDER BY date DESC").fetchall()
    conn.close()
    return [r['date'] for r in rows]


def get_model_settings(zone=None):
    ensure_runtime_schema()
    conn = _get_conn()
    if zone:
        row = conn.execute("SELECT * FROM model_settings WHERE zone=?", (zone,)).fetchone()
        result = dict(row) if row else dict(DEFAULT_MODEL_SETTINGS[zone])
    else:
        rows = conn.execute("SELECT * FROM model_settings ORDER BY zone").fetchall()
        result = {r['zone']: dict(r) for r in rows}
    conn.close()
    return result


def save_model_settings(zone, values):
    ensure_runtime_schema()
    allowed = [
        'warehouse_width_m', 'warehouse_depth_m', 'shelf_width_m',
        'shelf_depth_m', 'shelf_height_m', 'shelf_levels', 'aisle_percent',
        'floor_percent', 'workers', 'labor_efficiency', 'forklifts',
        'forklift_efficiency', 'inbound_pallets_day', 'outbound_orders_day',
        'avg_lines_order',
    ]
    current = get_model_settings(zone)
    merged = {key: values.get(key, current[key]) for key in allowed}
    merged['updated_at'] = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    assignments = ', '.join(f"{key}=?" for key in allowed) + ", updated_at=?"
    conn.execute(
        f"UPDATE model_settings SET {assignments} WHERE zone=?",
        tuple(merged[key] for key in allowed) + (merged['updated_at'], zone),
    )
    conn.commit()
    conn.close()
    return get_model_settings(zone)


def get_vfh_scenario(country='Demo-Europe'):
    ensure_runtime_schema()
    conn = _get_conn()
    row = conn.execute(
        'SELECT config_json,updated_at FROM vfh_scenarios WHERE country=?', (country,)
    ).fetchone()
    conn.close()
    result = dict(DEFAULT_VFH_SCENARIO)
    result['country'] = country
    if row:
        try:
            result.update(json.loads(row['config_json']))
        except (TypeError, json.JSONDecodeError):
            pass
        result['updated_at'] = row['updated_at']
    return result


def save_vfh_scenario(country, values):
    ensure_runtime_schema()
    current = get_vfh_scenario(country)
    allowed = set(DEFAULT_VFH_SCENARIO) - {'country'}
    merged = {key: values.get(key, current.get(key, DEFAULT_VFH_SCENARIO[key])) for key in allowed}
    merged['country'] = country
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    conn.execute('''
        INSERT INTO vfh_scenarios(country,config_json,updated_at) VALUES (?,?,?)
        ON CONFLICT(country) DO UPDATE SET config_json=excluded.config_json, updated_at=excluded.updated_at
    ''', (country, json.dumps(merged, ensure_ascii=False), now))
    conn.commit()
    conn.close()
    return get_vfh_scenario(country)


def get_inventory_batches(country='Demo-Europe'):
    """读取当前国家的批次库存；没有导入时返回空列表。"""
    ensure_runtime_schema()
    conn = _get_conn()
    rows = conn.execute('''
        SELECT country,batch_id,sku_code,receipt_date,qty,zone,status,unit_volume_m3,updated_at
        FROM inventory_batches
        WHERE country=? AND status='active'
        ORDER BY receipt_date,batch_id
    ''', (country,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def replace_inventory_batches(country, rows):
    """用一次CSV导入替换该国家的活动批次，避免重复累计。"""
    ensure_runtime_schema()
    now = datetime.now(timezone.utc).isoformat()
    normalized = []
    for row in rows:
        receipt_date = str(row['receipt_date']).strip()
        date.fromisoformat(receipt_date)
        normalized.append((
            country,
            str(row['batch_id']).strip(),
            str(row['sku_code']).strip(),
            receipt_date,
            int(row['qty']),
            str(row.get('zone') or 'A').strip().upper(),
            str(row.get('status') or 'active').strip().lower(),
            float(row['unit_volume_m3']) if row.get('unit_volume_m3') not in (None, '') else None,
            now,
        ))
    conn = _get_conn()
    with conn:
        conn.execute('DELETE FROM inventory_batches WHERE country=?', (country,))
        conn.executemany('''
            INSERT INTO inventory_batches(
                country,batch_id,sku_code,receipt_date,qty,zone,status,unit_volume_m3,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
        ''', normalized)
    conn.close()
    return get_inventory_batches(country)


def _auto_pallet_spec(length_cm, width_cm, height_cm):
    if any(float(value or 0) <= 0 for value in (length_cm, width_cm, height_cm)):
        return {
            'pallet_length_cm': 120,
            'pallet_width_cm': 80,
            'max_stack_height_cm': 180,
            'units_per_layer': 1,
            'layers_per_pallet': 1,
            'units_per_pallet': 1,
            'rotation_allowed': True,
        }
    length_cm = max(float(length_cm or 0), 1)
    width_cm = max(float(width_cm or 0), 1)
    height_cm = max(float(height_cm or 0), 1)
    normal = math.floor(120 / length_cm) * math.floor(80 / width_cm)
    rotated = math.floor(120 / width_cm) * math.floor(80 / length_cm)
    units_per_layer = max(1, normal, rotated)
    layers = max(1, math.floor((180 - 15) / height_cm))
    return {
        'pallet_length_cm': 120,
        'pallet_width_cm': 80,
        'max_stack_height_cm': 180,
        'units_per_layer': units_per_layer,
        'layers_per_pallet': layers,
        'units_per_pallet': units_per_layer * layers,
        'rotation_allowed': True,
    }


def get_sku_catalog(search='', limit=200, offset=0):
    ensure_runtime_schema()
    conn = _get_conn()
    latest = conn.execute("SELECT MAX(date) FROM daily_stock").fetchone()[0]
    like = f"%{search.strip()}%"
    rows = conn.execute('''
        SELECT c.*, COALESCE(SUM(ds.qty), 0) AS qty,
               COALESCE(GROUP_CONCAT(DISTINCT ds.zone), '') AS zones,
               p.pallet_length_cm, p.pallet_width_cm, p.max_stack_height_cm,
               p.units_per_layer, p.layers_per_pallet, p.units_per_pallet,
               p.rotation_allowed
        FROM sku_catalog c
        LEFT JOIN daily_stock ds
          ON ds.sku_code=c.code AND ds.date=?
        LEFT JOIN sku_pallet_specs p ON p.sku_code=c.code
        WHERE c.code LIKE ? OR c.name LIKE ? OR c.category LIKE ?
        GROUP BY c.code
        ORDER BY qty DESC, c.code
        LIMIT ? OFFSET ?
    ''', (latest, like, like, like, limit, offset)).fetchall()
    count = conn.execute('''
        SELECT COUNT(*) FROM sku_catalog
        WHERE code LIKE ? OR name LIKE ? OR category LIKE ?
    ''', (like, like, like)).fetchone()[0]
    conn.close()
    items = []
    for row in rows:
        item = dict(row)
        if item['units_per_pallet'] is None:
            item.update(_auto_pallet_spec(item['length_cm'], item['width_cm'], item['height_cm']))
        else:
            item['rotation_allowed'] = bool(item['rotation_allowed'])
        items.append(item)
    return {'items': items, 'total': count, 'date': latest}


def get_sku(code):
    result = get_sku_catalog(code, 200, 0)
    return next((item for item in result['items'] if item['code'] == code), None)


def save_sku(code, values):
    ensure_runtime_schema()
    current = get_sku(code)
    if not current:
        return None
    product_fields = ['name', 'category', 'length_cm', 'width_cm', 'height_cm', 'weight_kg', 'storage_type']
    merged = {key: values.get(key, current[key]) for key in product_fields}
    merged['volume_m3'] = round(
        float(merged['length_cm']) * float(merged['width_cm']) * float(merged['height_cm']) / 1_000_000,
        6,
    )
    spec_default = _auto_pallet_spec(merged['length_cm'], merged['width_cm'], merged['height_cm'])
    spec_fields = [
        'pallet_length_cm', 'pallet_width_cm', 'max_stack_height_cm',
        'units_per_layer', 'layers_per_pallet', 'units_per_pallet', 'rotation_allowed',
    ]
    spec = {key: values.get(key, current.get(key, spec_default[key])) for key in spec_fields}
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    conn.execute('''
        UPDATE sku_catalog SET name=?, category=?, length_cm=?, width_cm=?, height_cm=?,
        weight_kg=?, volume_m3=?, storage_type=? WHERE code=?
    ''', (
        merged['name'], merged['category'], merged['length_cm'], merged['width_cm'],
        merged['height_cm'], merged['weight_kg'], merged['volume_m3'],
        merged['storage_type'], code,
    ))
    conn.execute('''
        INSERT INTO sku_pallet_specs
        (sku_code,pallet_length_cm,pallet_width_cm,max_stack_height_cm,
         units_per_layer,layers_per_pallet,units_per_pallet,rotation_allowed,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sku_code) DO UPDATE SET
          pallet_length_cm=excluded.pallet_length_cm,
          pallet_width_cm=excluded.pallet_width_cm,
          max_stack_height_cm=excluded.max_stack_height_cm,
          units_per_layer=excluded.units_per_layer,
          layers_per_pallet=excluded.layers_per_pallet,
          units_per_pallet=excluded.units_per_pallet,
          rotation_allowed=excluded.rotation_allowed,
          updated_at=excluded.updated_at
    ''', (
        code, spec['pallet_length_cm'], spec['pallet_width_cm'],
        spec['max_stack_height_cm'], int(spec['units_per_layer']),
        int(spec['layers_per_pallet']), int(spec['units_per_pallet']),
        1 if spec['rotation_allowed'] else 0, now,
    ))
    conn.commit()
    conn.close()
    return get_sku(code)


def _legacy_load():
    """从旧的TSV文件读取（兼容）"""
    base = os.path.join(os.path.dirname(__file__), '..')
    path = os.path.join(base, 'stock (2).xls')
    if not os.path.exists(path):
        return []

    with open(path, 'rb') as f:
        data = f.read().decode('gbk')
    lines = data.strip().split('\n')
    stocks = []
    for line in lines[1:]:
        cols = line.split('\t')
        if len(cols) < 8: continue
        code = cols[2].strip('"').strip()
        qty_str = cols[7].strip('"').strip()
        if not code: continue
        try: qty = int(float(qty_str))
        except: qty = 0
        stocks.append({'code': code, 'name': '', 'category': '', 'length': 0, 'width': 0, 'height': 0, 'weight': 0, 'volume': 0, 'qty': qty, 'storage': 'flex'})
    stocks.sort(key=lambda x: -x['qty'])
    return stocks


ensure_runtime_schema()
