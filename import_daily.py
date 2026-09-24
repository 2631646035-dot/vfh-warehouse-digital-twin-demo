# -*- coding: utf-8 -*-
"""每日导入脚本 — 拖入一个 stock Excel/TSV/CSV 文件即可导入当天的库存数据"""
import os, sys, argparse
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), 'warehouse.db')


def detect_format(filepath):
    """自动检测文件格式：xlsx / GBK-TSV / CSV"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in ('.xlsx',):
        return 'xlsx'
    with open(filepath, 'rb') as f:
        head = f.read(500)
    try:
        head.decode('gbk')
        has_tabs = b'\t' in head
        return 'tsv_gbk' if has_tabs else 'csv'
    except:
        return 'csv'


def import_xlsx(conn, filepath, date_str):
    import openpyxl
    wb = openpyxl.load_workbook(filepath)
    ws = wb.active
    header_row = None
    col_map = {}
    for r in range(1, min(ws.max_row + 1, 10)):
        for c in range(1, ws.max_column + 1):
            v = str(ws.cell(r, c).value or '').strip()
            if v in ('货号', '货品名称', '可用数量', '库存数量', '库区类型', '区域'):
                header_row = r
                break
        if header_row:
            break
    if header_row is None:
        header_row = 1

    for c in range(1, ws.max_column + 1):
        v = str(ws.cell(header_row, c).value or '').strip()
        if '货号' in v: col_map['code'] = c
        elif '可用' in v or '库存数量' in v: col_map['qty'] = c
        elif '库区' in v or '区域' in v: col_map['zone_col'] = c

    if 'code' not in col_map:
        print("ERROR: Can't find '货号' column. Headers found:", list(col_map.keys()))
        wb.close()
        return 0

    count = 0
    start = header_row + 1
    for r in range(start, ws.max_row + 1):
        code = str(ws.cell(r, col_map['code']).value or '').strip().removesuffix('\t')
        if not code:
            continue
        qty = 0
        if 'qty' in col_map:
            try:
                qty = int(float(str(ws.cell(r, col_map['qty']).value or 0)))
            except:
                qty = 0
        zone = 'A'
        if 'zone_col' in col_map:
            zv = str(ws.cell(r, col_map['zone_col']).value or '')
            if 'vfh' in zv.lower() or 'b' in zv.lower() or zv == 'B':
                zone = 'B'

        conn.execute('''INSERT OR REPLACE INTO daily_stock(date, sku_code, zone, qty, storage_type)
                        VALUES (?,?,?,?,
                            COALESCE((SELECT storage_type FROM sku_catalog WHERE code=?), 'flexible'))''',
                     (date_str, code, zone, qty, code))
        count += 1
    wb.close()
    return count


def import_tsv(conn, filepath, date_str, encoding='gbk'):
    with open(filepath, 'rb') as f:
        data = f.read().decode(encoding)
    lines = data.strip().split('\n')
    count = 0
    for line in lines[1:]:
        cols = line.split('\t')
        if len(cols) < 8:
            continue
        code = cols[2].strip('"').strip()
        qty_str = cols[7].strip('"').strip()
        zone_raw = cols[4].strip('"').strip()
        zone = 'B' if (zone_raw and 'vfh' in zone_raw.lower()) else 'A'
        if not code:
            continue
        try:
            qty = int(float(qty_str))
        except:
            qty = 0
        conn.execute('''INSERT OR REPLACE INTO daily_stock(date, sku_code, zone, qty, storage_type)
                        VALUES (?,?,?,?,
                            COALESCE((SELECT storage_type FROM sku_catalog WHERE code=?), 'flexible'))''',
                     (date_str, code, zone, qty, code))
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description='导入每日库存数据到SQLite')
    parser.add_argument('file', help='要导入的Excel/TSV/CSV文件路径')
    parser.add_argument('--date', help='日期 (YYYY-MM-DD)，默认今天', default=None)
    parser.add_argument('--db', help='SQLite数据库路径', default=DB_PATH)
    args = parser.parse_args()

    if args.date is None:
        from datetime import date
        args.date = date.today().strftime('%Y-%m-%d')

    if not os.path.exists(args.file):
        print(f"ERROR: File not found: {args.file}")
        sys.exit(1)

    fmt = detect_format(args.file)
    print(f"File: {args.file}")
    print(f"Format: {fmt} | Date: {args.date}")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if fmt == 'xlsx':
        count = import_xlsx(conn, args.file, args.date)
    elif fmt == 'tsv_gbk':
        count = import_tsv(conn, args.file, args.date, encoding='gbk')
    else:
        count = import_tsv(conn, args.file, args.date, encoding='utf-8')

    conn.commit()
    conn.close()

    print(f"[OK] {count} records imported for {args.date}")


if __name__ == '__main__':
    main()
