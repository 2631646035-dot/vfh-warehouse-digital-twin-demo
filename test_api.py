import urllib.request, json

def test(path):
    r = urllib.request.urlopen(f"http://localhost:8080{path}")
    return json.loads(r.read())

print("=== /api/summary ===")
d = test("/api/summary")
print(f"日期: {d['date']} | SKU数: {d['total_skus']} | 总库存: {d['total_units']}台")
print(f"只放地堆: {d['floor_only']}台 | 灵活存放: {d['flexible']}台")
print()
print("品类分布:")
for k, v in sorted(d['by_category'].items(), key=lambda x: -x[1]['total']):
    print(f"  {k}: {v['total']}台 (地堆{v['floor']}/灵活{v['flex']})")

print()
print("=== /api/dates ===")
print(f"已有日期: {test('/api/dates')['dates']}")

print()
print("=== /api/stocks (前10) ===")
stocks = test("/api/stocks")
for s in stocks[:10]:
    print(f"  {s['code']:25s} | {s['category']:6s} | qty={s['qty']:5d} | vol={s['volume']:.4f}m³ | {s['storage']}")

print()
print("=== /api/warehouse ===")
cfg = test("/api/warehouse")
print(f"A区货架: {cfg['zone_a']['shelf_rows']}排×{cfg['zone_a']['shelf_cols']}列 已用{cfg['zone_a']['shelf_used']} | 地堆: {cfg['zone_a']['floor_area']}m²")
print(f"B区货架: {cfg['zone_b']['shelf_rows']}排×{cfg['zone_b']['shelf_cols']}列 已用{cfg['zone_b']['shelf_used']} | 地堆: {cfg['zone_b']['floor_area']}m² {cfg['zone_b']['floor_pallets']}托")
