"""夜测事件窗口分析:从 docs/路测数据_20260715_2030-2130.csv 提取各事件前后数据。

字段口径(按 0715 早测结论,与协议文档相反):
  CSV 的 gyro_x/y/z 实为加速度计原始值(16384 LSB = 1g)
  CSV 的 acc_x/y/z  实为陀螺仪(静止≈0,事件时突跳)
"""
import csv, glob, math, statistics

path = glob.glob('docs/*2030-2130.csv')[0]
rows = []
with open(path, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        t = r['gps_time'][11:]
        sec = int(t[0:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8])
        rows.append(dict(
            t=t, sec=sec, v=float(r['speed']),
            ax=int(r['gyro_x']), ay=int(r['gyro_y']), az=int(r['gyro_z']),
            gx=int(r['acc_x']), gy=int(r['acc_y']), gz=int(r['acc_z'])))

# 直立基准:开头静止段的中位数
base = [r for r in rows if '20:30:00' <= r['t'] <= '20:31:30']
ref = tuple(statistics.median([r[k] for r in base]) for k in ('ax', 'ay', 'az'))
nref = math.sqrt(sum(x * x for x in ref))

def ang(r):
    dot = r['ax'] * ref[0] + r['ay'] * ref[1] + r['az'] * ref[2]
    na = math.sqrt(r['ax'] ** 2 + r['ay'] ** 2 + r['az'] ** 2)
    if na * nref == 0:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / (na * nref)))))

def amag(r):
    return math.sqrt(r['ax'] ** 2 + r['ay'] ** 2 + r['az'] ** 2)

def gmag(r):
    return math.sqrt(r['gx'] ** 2 + r['gy'] ** 2 + r['gz'] ** 2)

EVENTS = [
    ('20:38', '向右摔车'), ('20:39', '前向轻微碰撞'), ('20:41', '向左摔车'),
    ('20:42', '向前摔车→随后向左倾倒'), ('20:44', '急刹车'), ('20:46', '向左摔车'),
    ('20:49', '急刹车+等红绿灯'), ('20:54', '下坡路段'), ('20:59', '上坡'),
    ('21:02', '上坡+开始等餐'), ('21:21', '取餐开始配送'), ('21:23', '颠簸路段'),
]

out = [f'直立基准加速度矢量 ref={ref} |ref|={nref:.0f} (16384=1g)']
for ts, name in EVENTS:
    h, m = map(int, ts.split(':'))
    t0, t1 = h * 3600 + m * 60 - 25, h * 3600 + m * 60 + 115
    win = [r for r in rows if t0 <= r['sec'] <= t1]
    out.append(f'===== {ts} {name} =====')
    if not win:
        out.append('(无数据)')
        continue
    mx = max(win, key=ang)
    out.append(f'摘要: 点数={len(win)} 倾角峰值={ang(mx):.0f}°@{mx["t"]} '
               f'陀螺峰值|g|={max(gmag(r) for r in win):.0f} v范围={min(r["v"] for r in win):.0f}~{max(r["v"] for r in win):.0f}')
    for r in win:
        out.append(f"{r['t']} v={r['v']:5.1f} 倾角={ang(r):5.1f} "
                   f"a=({r['ax']:6d},{r['ay']:6d},{r['az']:6d})|{amag(r):5.0f}| "
                   f"g=({r['gx']:6d},{r['gy']:6d},{r['gz']:6d})|{gmag(r):5.0f}|")

# 附:全程逐分钟倾角/速度/陀螺概览(坡道与颠簸用)
out.append('===== 逐分钟概览 (中位倾角 / 中位速度 / |a|标准差 / 陀螺|g|峰值) =====')
byminute = {}
for r in rows:
    byminute.setdefault(r['t'][:5], []).append(r)
for mkey in sorted(byminute):
    g = byminute[mkey]
    angs = sorted(ang(r) for r in g)
    vs = sorted(r['v'] for r in g)
    mags = [amag(r) for r in g]
    std = statistics.pstdev(mags) if len(mags) > 1 else 0
    out.append(f'{mkey} 倾角={angs[len(angs)//2]:5.1f} v={vs[len(vs)//2]:5.1f} '
               f'|a|std={std:6.0f} gmax={max(gmag(r) for r in g):6.0f} n={len(g)}')

open('_analysis_out.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('done rows=', len(rows))
