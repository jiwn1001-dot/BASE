"""
cogs/_match_engine.py — 축구/야구 경기 시뮬레이션 엔진 (디스코드 의존성 없음)
경기 전체를 미리 시뮬레이션해 '이벤트 타임라인'으로 돌려주고,
sports.py 가 그 타임라인을 5분 동안 실시간으로 풀어서 중계한다.
(파일명이 _ 로 시작하므로 app.py 의 Cog 자동 로드 대상에서 제외됨)
"""

import math
import random
import re
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════
#  공통 유틸
# ═══════════════════════════════════════════════════════════════
def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _top(players, key, n):
    return sorted(players, key=key, reverse=True)[:n]


def _pick(rng, players, weight):
    return rng.choices(players, weights=[max(weight(p), 0.01) for p in players], k=1)[0]


_JOSA = re.compile(r"([가-힣A-Za-z0-9])(\*\*)?\((이|을|은|과|으)\)(가|를|는|와|로)")


def fix_josa(text):
    """'**구자룡**(이)가' → '**구자룡**이' 처럼 받침에 맞춰 조사를 고른다."""
    def sub(m):
        ch, bold, a, b = m.group(1), m.group(2) or "", m.group(3), m.group(4)
        if "가" <= ch <= "힣":
            jong = (ord(ch) - 0xAC00) % 28
            has = jong != 0 and not (a == "으" and jong == 8)  # ㄹ받침 + (으)로 → 로
        else:
            has = False
        if a == "으":
            return ch + bold + ("으로" if has else "로")
        return ch + bold + (a if has else b)
    return _JOSA.sub(sub, text) if text else text


def _lbl(label, tmpl):
    """'🧤 본문' 템플릿을 '🧤 67' | 본문' 형태로"""
    emoji, rest = tmpl.split(" ", 1)
    return f"{emoji} {label} | {rest}"


def _dummy_roster(team, stats, n):
    return [dict({"player_name": f"{team} 선수{i + 1}"}, **stats) for i in range(n)]


# ═══════════════════════════════════════════════════════════════
#  ⚽ 축구
# ═══════════════════════════════════════════════════════════════
SOCCER_STATS = ("pace", "shooting", "passing", "dribbling", "defending", "physical")


@dataclass
class SoccerEvent:
    minute: int              # 경기 분 (추가시간 포함 절대값)
    half: int                # 1 / 2
    label: str               # "67'" / "45+2'"
    kind: str                # ko ht ft goal save post miss var pen_miss yellow red chance flavor stoppage
    text: str
    team: int | None = None  # 0 = 홈, 1 = 원정
    buildup: str | None = None
    alert: str | None = None
    score: tuple = (0, 0)
    shots: tuple = (0, 0)
    on_target: tuple = (0, 0)
    poss: float = 50.0       # 홈 점유율(%)
    scorer: str | None = None
    assist: str | None = None


@dataclass
class SoccerMatch:
    home: str
    away: str
    events: list
    score: tuple
    stoppage: tuple          # (전반 추가시간, 후반 추가시간)
    ratings: dict = field(default_factory=dict)


def soccer_team_profile(players, team):
    players = [dict(p) for p in players] or _dummy_roster(team, {s: 55 for s in SOCCER_STATS}, 11)
    ovr = lambda p: _mean(p[s] for s in SOCCER_STATS)
    gk = max(players, key=lambda p: p["defending"] + p["physical"] * 0.4 - p["shooting"] * 0.9 - p["pace"] * 0.3)
    outfield = _top([p for p in players if p is not gk], ovr, 10) or [gk]
    bench = sorted([p for p in players if p is not gk and p not in outfield], key=ovr, reverse=True)[:7]
    prof = {
        "name": team,
        "gk": gk,
        "outfield": outfield,
        "bench": bench,
        "gk_r": 0.7 * gk["defending"] + 0.3 * gk["physical"],
        "ovr": _mean(ovr(p) for p in outfield + [gk]),
    }
    prof.update(_soccer_lines(outfield))
    return prof


_ATT_R = lambda p: 0.4 * p["shooting"] + 0.25 * p["dribbling"] + 0.2 * p["pace"] + 0.15 * p["passing"]
_MID_R = lambda p: 0.5 * p["passing"] + 0.3 * p["dribbling"] + 0.2 * p["physical"]
_DEF_R = lambda p: 0.65 * p["defending"] + 0.35 * p["physical"]


def _soccer_lines(outfield):
    """현재 그라운드 위 선수들로 공격/중원/수비 전력 계산 (교체·퇴장 시 재계산)"""
    return {
        "att": _mean(_ATT_R(p) for p in _top(outfield, _ATT_R, 5)),
        "mid": _mean(_MID_R(p) for p in _top(outfield, _MID_R, 5)),
        "dfn": _mean(_DEF_R(p) for p in _top(outfield, _DEF_R, 4)),
    }


GOAL_TEXTS = [
    "**{p}**의 강력한 슈팅이 골망을 찢습니다!!",
    "**{p}**, 수비 둘을 벗겨내고 침착하게 밀어 넣습니다! 골!!",
    "**{p}**의 헤더!! 크로스를 그대로 꽂아 넣습니다!",
    "**{p}**의 중거리포!! 골키퍼가 손도 못 댑니다!",
    "**{p}**, 문전 혼전 상황에서 끝까지 집중해 밀어 넣습니다!",
    "**{p}**의 원터치 마무리! 교과서 같은 역습 골!",
    "**{p}**의 칩슛!! 골키퍼 키를 넘깁니다!",
]
ASSIST_TEXTS = ["도움: **{a}**", "**{a}**의 킬패스가 만든 골", "**{a}**의 크로스가 일품이었습니다"]
BUILDUP_TEXTS = [
    "**{p}**, 박스 안으로 파고듭니다…!",
    "**{p}** 앞에 공간이 열립니다! 슈팅 각도가…!",
    "**{p}**에게 연결되는 스루패스…! 골키퍼와 1:1!",
    "측면 크로스…! 문전에 **{p}** 쇄도합니다!",
    "**{p}**, 페널티 아크 정면에서 발을 뒤로 뺍니다…!",
    "세컨볼이 **{p}** 발 앞에 떨어집니다…!",
]
SAVE_TEXTS = [
    "🧤 **{g}** 슈퍼세이브!! **{p}**의 슈팅을 막아냅니다!",
    "🧤 **{p}**의 회심의 슈팅… **{g}**(이)가 몸을 날려 쳐냅니다!",
    "🧤 **{g}**, 동물적인 반사신경! **{p}**의 슈팅을 걷어냅니다!",
]
POST_TEXTS = [
    "🥅 **{p}**의 슈팅이 골대를 강타!!! 아아악!",
    "🥅 **{p}**의 감아차기… 크로스바를 때립니다!!",
]
MISS_TEXTS = [
    "💨 **{p}**의 슈팅, 골대를 살짝 벗어납니다.",
    "💨 **{p}**의 슈팅이 수비 몸에 맞고 굴절됩니다.",
    "💨 **{p}**, 하늘로 날려버립니다… 관중석이 탄식합니다.",
]
FLAVOR_TEXTS = [
    "🌀 **{p}**의 현란한 드리블! 수비 둘을 달고 전진합니다.",
    "🎯 **{p}**의 롱패스가 반대편 측면으로 정확히 떨어집니다.",
    "🛡️ **{d}**의 깔끔한 태클! 위험 지역에서 공을 끊어냅니다.",
    "🚩 **{t}** 코너킥 찬스를 얻어냅니다.",
    "💨 **{p}**의 스피드! 측면을 완전히 허뭅니다.",
    "📣 **{t}** 서포터즈의 응원 소리가 경기장을 가득 채웁니다.",
    "🔁 **{t}**, 후방에서 차분하게 빌드업을 이어갑니다.",
]
MOMENTUM_TEXTS = [
    "🔥 **{t}**, 경기 주도권을 완전히 잡습니다! 파상공세!",
    "🌊 **{t}**의 압박이 거세집니다! 상대가 자기 진영에 갇혔습니다!",
    "⚡ 흐름이 바뀝니다! **{t}**, 연이어 위협적인 장면을 만들어냅니다!",
]
SOCCER_TUNE = {"슈팅배율": 1.0, "득점배율": 1.0, "홈어드밴티지": 0.03, "카드배율": 1.0, "PK배율": 1.0, "흐름변화": 1.0}


def simulate_soccer(home, away, home_players, away_players, rng=None, tune=None, form=(0.0, 0.0)) -> SoccerMatch:
    """
    tune: SOCCER_TUNE 형식 배율 (관리자 설정)
    form: 팀별 기세 -1.0(연패) ~ +1.0(연승) — 공격/수비 ±4%
    """
    rng = rng or random.Random()
    tn = dict(SOCCER_TUNE, **(tune or {}))
    T = [soccer_team_profile(home_players, home), soccer_team_profile(away_players, away)]
    boost = [1 + 0.04 * f for f in form]
    names = [home, away]
    score = [0, 0]
    shots = [0, 0]
    on_t = [0, 0]
    poss_acc = [0.0, 0.0]
    yellows: dict[str, int] = {}
    red_factor = [1.0, 1.0]
    active = [list(T[0]["outfield"]), list(T[1]["outfield"])]
    bench = [list(T[0]["bench"]), list(T[1]["bench"])]
    lines_ = [_soccer_lines(active[0]), _soccer_lines(active[1])]
    events: list[SoccerEvent] = []
    st = (rng.choice([0, 1, 1, 2, 2, 3]), rng.choice([2, 3, 4, 4, 5, 6, 7]))
    # 교체 타이밍 (후반 55~85분, 벤치 인원만큼 최대 3회)
    sub_at = [sorted(rng.sample(range(55, 86), min(3, len(bench[k])))) for k in (0, 1)]
    mom = {"team": None, "until": 0}

    def poss():
        tot = poss_acc[0] + poss_acc[1]
        return round(100 * poss_acc[0] / tot, 1) if tot else 50.0

    def add(minute, half, label, kind, text, team=None, **kw):
        for k in ("buildup", "alert"):
            kw[k] = fix_josa(kw.get(k))
        events.append(SoccerEvent(minute, half, label, kind, fix_josa(text), team,
                                  score=tuple(score), shots=tuple(shots), on_target=tuple(on_t),
                                  poss=poss(), **kw))

    add(0, 1, "0'", "ko", f"🟢 킥오프! **{home}**(홈) vs **{away}**(원정), 경기 시작합니다!")

    for half in (1, 2):
        base = 0 if half == 1 else 45
        length = 45 + st[half - 1]
        if half == 2:
            add(45, 2, "46'", "ko", "🟢 후반전 킥오프!")
        for m in range(base + 1, base + length + 1):
            reg_end = 45 if half == 1 else 90
            label = f"{m}'" if m <= reg_end else f"{reg_end}+{m - reg_end}'"
            if m == reg_end + 1 and st[half - 1] > 0:
                add(m, half, label, "stoppage", f"⏱️ 추가시간 **{st[half - 1]}분**이 주어집니다!")

            # ── 교체 ──
            for k in (0, 1):
                if half == 2 and sub_at[k] and m >= sub_at[k][0] and bench[k] and len(active[k]) > 1:
                    sub_at[k].pop(0)
                    diff = score[k] - score[1 - k]
                    if diff < 0:
                        inn = max(bench[k], key=lambda p: p["shooting"])
                        out = min(active[k], key=lambda p: p["shooting"])
                        why = " — 공격 카드 투입!"
                    elif diff > 0:
                        inn = max(bench[k], key=lambda p: p["defending"])
                        out = max(active[k], key=lambda p: p["shooting"] - p["defending"])
                        why = " — 굳히기에 들어갑니다."
                    else:
                        inn = max(bench[k], key=lambda p: _mean(p[s_] for s_ in SOCCER_STATS))
                        out = min(active[k], key=lambda p: p["physical"] + rng.uniform(0, 15))
                        why = ""
                    bench[k].remove(inn)
                    active[k][active[k].index(out)] = inn
                    lines_[k] = _soccer_lines(active[k])
                    add(m, half, label, "sub",
                        f"🔄 {label} | **{names[k]}** 교체: {out['player_name']} ▶ **{inn['player_name']}**{why}", k)

            # ── 흐름(모멘텀) ──
            if mom["team"] is not None and m >= mom["until"]:
                mom["team"] = None
            if mom["team"] is None and rng.random() < 0.025 * tn["흐름변화"]:
                k = 0 if rng.random() < 0.5 + (lines_[0]["mid"] - lines_[1]["mid"]) / 100 else 1
                mom.update(team=k, until=m + rng.randint(6, 12))
                add(m, half, label, "momentum", _lbl(label, rng.choice(MOMENTUM_TEXTS).format(t=names[k])), k)

            # ── 점유 ──
            mid0, mid1 = lines_[0]["mid"] * boost[0], lines_[1]["mid"] * boost[1]
            ctrl0 = mid0 ** 2 / (mid0 ** 2 + mid1 ** 2) + tn["홈어드밴티지"]
            if mom["team"] is not None:
                ctrl0 += 0.12 if mom["team"] == 0 else -0.12
            ctrl0 = min(max(ctrl0 * red_factor[0] / (ctrl0 * red_factor[0] + (1 - ctrl0) * red_factor[1]), 0.15), 0.85)
            ctrl = [ctrl0, 1 - ctrl0]
            poss_acc[0] += ctrl0 + rng.uniform(-0.1, 0.1)
            poss_acc[1] += 1 - ctrl0

            shot_this_minute = False
            for i in (0, 1):
                j = 1 - i
                # 경기 상황에 따른 공격 성향: 지고 있는 팀은 후반 막판 총공세
                urg = 1.0
                if m >= 60:
                    diff = score[i] - score[j]
                    if diff < 0:
                        urg = 1 + 0.18 * min(2, -diff) * (m - 55) / 35
                    elif diff > 0:
                        urg = 0.88
                ratio = lines_[i]["att"] * boost[i] / max(lines_[j]["dfn"] * boost[j], 1)
                surge = 1.3 if mom["team"] == i else 1.0
                p_shot = 0.135 * (2 * ctrl[i]) * ratio ** 2.2 * urg * red_factor[i] * surge * tn["슈팅배율"]
                p_pen = 0.0016 * ratio * urg * tn["PK배율"]

                if rng.random() < p_pen:
                    shooter = max(active[i], key=lambda p: p["shooting"])
                    fouler = _pick(rng, active[j], lambda p: p["defending"])
                    shots[i] += 1
                    conv = min(0.92, max(0.6, 0.76 + (shooter["shooting"] - 75) / 300))
                    bu = (f"🚨 {label} | 박스 안에서 **{fouler['player_name']}**의 태클… 주심 휘슬!! **페널티킥!!**\n"
                          f"키커는 **{shooter['player_name']}**… 골키퍼 **{T[j]['gk']['player_name']}**와 마주 섭니다…")
                    if rng.random() < conv:
                        score[i] += 1
                        on_t[i] += 1
                        add(m, half, label, "goal",
                            f"⚽ {label} | **{shooter['player_name']}** PK 성공!! 골키퍼를 완벽히 속입니다! ({names[i]})",
                            i, buildup=bu, scorer=shooter["player_name"],
                            alert=_goal_alert(label, names, score, i, shooter["player_name"], m, "PK"))
                    else:
                        if rng.random() < 0.7:
                            on_t[i] += 1
                        add(m, half, label, "pen_miss",
                            f"❌ {label} | **{T[j]['gk']['player_name']}** PK 선방!!! **{shooter['player_name']}**의 킥을 막아냅니다!!",
                            i, buildup=bu, alert=f"🧤 **PK 선방!!** {label} {T[j]['gk']['player_name']} ({names[j]})")
                    shot_this_minute = True
                    continue

                if rng.random() >= p_shot:
                    continue
                shot_this_minute = True
                shooter = _pick(rng, active[i], lambda p: (p["shooting"] / 50) ** 3)
                resist = 0.5 * T[j]["gk_r"] + 0.5 * lines_[j]["dfn"]
                xg = 0.12 * tn["득점배율"] * (shooter["shooting"] / resist) ** 2 * rng.lognormvariate(-0.18, 0.6)
                xg = min(max(xg, 0.02), 0.65)
                shots[i] += 1
                p = shooter["player_name"]
                g = T[j]["gk"]["player_name"]
                big = xg >= 0.17
                bu = f"⚡ {label} | " + rng.choice(BUILDUP_TEXTS).format(p=p) if big else None
                r = rng.random()
                if r < xg:
                    on_t[i] += 1
                    if rng.random() < 0.05:
                        add(m, half, label, "var",
                            f"📺 {label} | **{p}**의 골… 그러나 VAR 판독 결과 **오프사이드!!** 골이 취소됩니다!",
                            i, buildup=bu or f"⚡ {label} | **{p}**의 슈팅… 골망이 흔들립니다!! 그런데 주심이 귀에 손을 댑니다…",
                            alert=f"📺 **VAR 골 취소!** {label} {p} ({names[i]})")
                        continue
                    score[i] += 1
                    others = [x for x in active[i] if x is not shooter]
                    text = f"⚽ {label} | " + rng.choice(GOAL_TEXTS).format(p=p)
                    assist = None
                    if others and rng.random() < 0.75:
                        assist = _pick(rng, others, lambda x: (x["passing"] / 50) ** 3)["player_name"]
                        text += " — " + rng.choice(ASSIST_TEXTS).format(a=assist)
                    add(m, half, label, "goal", text, i,
                        buildup=bu or f"⚡ {label} | **{p}**의 슈팅…!",
                        scorer=p, assist=assist, alert=_goal_alert(label, names, score, i, p, m))
                elif r < xg + (1 - xg) * 0.33:
                    on_t[i] += 1
                    add(m, half, label, "save", _lbl(label, rng.choice(SAVE_TEXTS).format(p=p, g=g)), i, buildup=bu)
                elif r < xg + (1 - xg) * 0.37:
                    add(m, half, label, "post", _lbl(label, rng.choice(POST_TEXTS).format(p=p)), i,
                        buildup=bu or f"⚡ {label} | **{p}**, 먼 거리에서 감아 찹니다…!")
                else:
                    add(m, half, label, "miss", _lbl(label, rng.choice(MISS_TEXTS).format(p=p)), i, buildup=bu)

            # ── 카드 ──
            if rng.random() < 0.03 * tn["카드배율"]:
                i = 0 if rng.random() < ctrl[1] else 1  # 공 없는 팀이 반칙
                pl = _pick(rng, active[i], lambda p: p["defending"] + p["physical"])
                nm = pl["player_name"]
                straight = rng.random() < 0.02
                yellows[nm] = yellows.get(nm, 0) + (0 if straight else 1)
                if straight or yellows[nm] >= 2:
                    why = "다이렉트 퇴장!!" if straight else "경고 누적 퇴장!!"
                    if len(active[i]) > 1:
                        active[i].remove(pl)
                        lines_[i] = _soccer_lines(active[i])
                    red_factor[i] *= 0.84
                    add(m, half, label, "red", f"🟥 {label} | **{nm}** {why} **{names[i]}** 10명으로 싸웁니다!", i,
                        buildup=f"😡 {label} | **{nm}**의 거친 태클… 주심이 달려옵니다…",
                        alert=f"🟥 **퇴장!** {label} {nm} ({names[i]})")
                else:
                    add(m, half, label, "yellow", f"🟨 {label} | **{nm}**에게 옐로카드.", i)
            elif not shot_this_minute and rng.random() < 0.09:
                i = 0 if rng.random() < ctrl[0] else 1
                p = _pick(rng, active[i], lambda x: x["dribbling"] + x["passing"])
                d = _pick(rng, active[1 - i], lambda x: x["defending"])
                add(m, half, label, "flavor",
                    _lbl(label, rng.choice(FLAVOR_TEXTS).format(p=p["player_name"], d=d["player_name"], t=names[i])), i)

        if half == 1:
            add(45 + st[0], 1, "HT", "ht", f"⏸️ **전반 종료** — {home} {score[0]} : {score[1]} {away}")

    if score[0] > score[1]:
        res = f"🏆 **{home}** 승리!"
    elif score[1] > score[0]:
        res = f"🏆 **{away}** 승리!"
    else:
        res = "🤝 무승부!"
    add(90 + st[1], 2, "FT", "ft", f"🏁 **경기 종료!!** {home} {score[0]} : {score[1]} {away} — {res}")
    return SoccerMatch(home, away, events, tuple(score), st,
                       {"home": T[0], "away": T[1]})


def _goal_alert(label, names, score, i, scorer, minute, tag=""):
    j = 1 - i
    head = "⚽ **GOAL!!!**"
    if minute >= 85:
        if score[i] == score[j]:
            head = "🔥🔥 **극장 동점골!!!**"
        elif score[i] == score[j] + 1:
            head = "🔥🔥🔥 **극장 결승골!!!**"
    elif score[i] == score[j] and score[i] > 0:
        head = "⚽ **동점골!!!**"
    elif score[i] == score[j] + 1 and score[j] > 0:
        head = "⚽ **다시 앞서가는 골!!!**"
    tag = f" ({tag})" if tag else ""
    return (f"{head} {label} **{scorer}**{tag}\n"
            f"**{names[0]} {score[0]} : {score[1]} {names[1]}**")


# ═══════════════════════════════════════════════════════════════
#  ⚾ 야구
# ═══════════════════════════════════════════════════════════════
BASEBALL_STATS = ("contact", "power", "run", "arm", "field")
MAX_INNINGS = 12
# 리그 평균 기준치 (player_data 기준) — 능력치를 이 값 대비 편차로 해석
C0, P0, R0, A0, F0 = 76, 66, 63, 90, 81


@dataclass
class BaseballEvent:
    kind: str                # pa info change inning end
    text: str
    inning: int
    top: bool
    outs: int
    bases: tuple             # (1루, 2루, 3루) 주자 이름 또는 None
    score: tuple             # (원정, 홈)
    runs: int = 0            # 이 이벤트로 공격팀이 낸 점수
    hit: bool = False
    batter: str | None = None
    res: str | None = None   # 타석 결과 코드: K BB HBP 1B 2B 3B HR E OUT
    pitcher: str | None = None
    pitches: int = 0         # 현재 투수 투구수
    buildup: str | None = None
    alert: str | None = None


@dataclass
class BaseballMatch:
    away: str
    home: str
    events: list
    score: tuple             # (원정, 홈)
    innings: int
    ratings: dict = field(default_factory=dict)


def baseball_team_profile(players, team, starter_idx=0):
    players = [dict(p) for p in players] or _dummy_roster(
        team, {"contact": 65, "power": 55, "run": 55, "arm": 80, "field": 70}, 12)
    bat = lambda p: p["contact"] * 0.55 + p["power"] * 0.45
    nine = _top(players, bat, 9)
    while len(nine) < 9:  # 선수 부족 시 대타 투입
        nine.append(dict(nine[len(nine) % max(len(nine), 1)] if nine else players[0]))
    # 타순: 1번 = 발+컨택, 4번 = 파워, 3번 = 나머지 최고 타자
    order = sorted(nine, key=bat, reverse=True)
    lead = max(order, key=lambda p: p["run"] + p["contact"])
    order.remove(lead)
    clean = max(order, key=lambda p: p["power"])
    order.remove(clean)
    third = order.pop(0)
    lineup = [lead, order.pop(0), third, clean] + order
    # 투수진: 타선에 없는 선수 우선, 송구(arm) 순
    bench = [p for p in players if p not in nine]
    staff = sorted(bench, key=lambda p: p["arm"], reverse=True)
    if len(staff) < 4:
        staff += sorted([p for p in nine if p not in staff], key=lambda p: p["arm"], reverse=True)[: 4 - len(staff)]
    # 선발 로테이션 (최대 5인) — 경기마다 다음 선발이 등판
    rotation = staff[:5] if len(staff) >= 7 else staff[: max(1, len(staff) - 2)]
    starter = rotation[starter_idx % len(rotation)]
    rest = [p for p in staff if p not in rotation]
    closer = rest[0] if rest else next((p for p in staff if p is not starter), starter)
    return {
        "name": team,
        "lineup": lineup,
        "starter": starter,
        "rotation": rotation,
        "closer": closer,
        "bullpen": [p for p in staff if p is not starter and p is not closer] or [closer],
        "field": _mean(p["field"] for p in nine),
        "c_arm": _mean(p["arm"] for p in nine),
        "bat": _mean(bat(p) for p in lineup),
        "pit": staff[0]["arm"],
    }


HIT_TEXTS = {
    "1B": ["**{b}**, 깨끗한 중전 안타!", "**{b}**의 타구가 유격수 옆을 빠져나갑니다! 안타!",
           "**{b}**, 밀어쳐서 우전 안타!", "**{b}**의 빗맞은 타구… 코스가 좋습니다! 안타!"],
    "2B": ["**{b}**의 좌중간을 가르는 2루타!!", "**{b}**, 펜스 앞 원바운드! 2루타!", "**{b}**의 라인드라이브가 1루선을 타고 흐릅니다! 2루타!"],
    "3B": ["**{b}**의 우중간 깊숙한 타구!! 3루까지 전력질주!! 3루타!"],
    "HR": ["**{b}**, 풀스윙!!! 넘어갔습니다!!! 홈런!!! 🎆", "**{b}**의 타구가 담장을 훌쩍 넘깁니다!! 홈런!!",
           "**{b}**, 맞는 순간 확신! 비거리 130m 대형 홈런!!! 🎆"],
}
OUT_TEXTS = {
    "K": ["**{b}** 헛스윙 삼진! **{p}**의 결정구가 춤을 춥니다.", "**{b}** 루킹 삼진! 꼼짝 못 합니다.",
          "**{p}**, 150km 강속구로 **{b}**(을)를 돌려세웁니다! 삼진!"],
    "GB": ["**{b}** 땅볼, **{f}**의 깔끔한 처리로 아웃.", "**{b}**의 강한 땅볼… **{f}**(이)가 몸을 날려 잡아냅니다! 아웃!"],
    "FB": ["**{b}**의 높이 뜬 공, **{f}**(이)가 여유 있게 잡아냅니다.", "**{b}**의 큰 타구!! …워닝트랙에서 **{f}**(이)가 잡아냅니다!"],
    "LD": ["**{b}**의 총알 같은 직선타… **{f}** 정면! 아웃!"],
    "PU": ["**{b}** 내야 뜬공. **{f}**(이)가 콜하고 잡습니다."],
}


def _bases_desc(bases):
    on = [i for i, b in enumerate(bases) if b]
    if len(on) == 3:
        return "만루"
    if not on:
        return "주자 없음"
    return ",".join(f"{i + 1}" for i in on) + "루"


BASEBALL_TUNE = {"안타배율": 1.0, "홈런배율": 1.0, "삼진배율": 1.0, "볼넷배율": 1.0, "도루배율": 1.0, "홈어드밴티지": 0.1}


def simulate_baseball(away, home, away_players, home_players, rng=None, tune=None,
                      starters=(0, 0), form=(0.0, 0.0)) -> BaseballMatch:
    """
    starters: (원정, 홈) 선발 로테이션 순번
    form: 팀별 기세 -1.0 ~ +1.0 — 타격 보정
    """
    rng = rng or random.Random()
    tn = dict(BASEBALL_TUNE, **(tune or {}))
    T = [baseball_team_profile(away_players, away, starters[0]),
         baseball_team_profile(home_players, home, starters[1])]
    names = [away, home]
    score = [0, 0]
    order_idx = [0, 0]
    events: list[BaseballEvent] = []
    # 각 팀의 현재 투수 상태
    pit = [{"p": T[k]["starter"], "bf": 0, "ra": 0, "pc": 0, "used": {T[k]["starter"]["player_name"]}}
           for k in (0, 1)]
    bullpen = [list(T[k]["bullpen"]) for k in (0, 1)]
    closer_used = [False, False]

    def add(kind, text, inning, top, outs, bases, **kw):
        for k in ("buildup", "alert"):
            kw[k] = fix_josa(kw.get(k))
        events.append(BaseballEvent(kind, fix_josa(text), inning, top, outs, tuple(bases), tuple(score), **kw))

    def maybe_change(dk, inning, top, outs, bases, lead):
        """dk = 수비팀 인덱스"""
        st = pit[dk]
        cur = st["p"]
        new = None
        closer = T[dk]["closer"]
        if (inning >= 9 and 1 <= lead <= 3 and not closer_used[dk]
                and cur["player_name"] != closer["player_name"] and outs == 0 and not any(bases)):
            new, why = closer, "세이브 상황, 마무리 투수 투입!"
            closer_used[dk] = True
        else:
            limit = 95 + (cur["arm"] - A0) * 1.5 + st.setdefault("jit", rng.randint(-8, 10))
            if cur is T[dk]["starter"] and (st["pc"] >= limit or (st["ra"] >= 5 and st["pc"] >= 40)):
                why = (f"선발 강판! ({st['pc']}구 {st['ra']}실점)" if st["ra"] >= 5
                       else f"선발 {st['pc']}구 역투, 불펜 가동!")
            elif cur is not T[dk]["starter"] and cur is not closer and st["pc"] >= 22 + rng.randint(0, 12):
                why = "불펜 교체."
            else:
                return
            pool = [p for p in bullpen[dk] if p["player_name"] not in st["used"]]
            if not pool:  # 불펜 소진 → 쓸 수 있는 아무 투수나 재투입
                pool = [p for p in bullpen[dk] + [T[dk]["closer"]] if p["player_name"] != cur["player_name"]]
                if not pool:
                    return
            new = pool[0]
        st.update(p=new, bf=0, ra=0, pc=0, jit=rng.randint(-8, 10))
        st["used"].add(new["player_name"])
        add("change", f"🔄 **{names[dk]}** 투수 교체: {cur['player_name']} → **{new['player_name']}** ({why})",
            inning, top, outs, bases, pitcher=new["player_name"])

    def half_inning(inning, top):
        bk = 0 if top else 1   # 공격팀
        dk = 1 - bk
        outs = 0
        bases = [None, None, None]
        half = "초" if top else "말"
        tag = f"{inning}회{half}"
        walkoff_possible = (not top) and inning >= 9
        add("inning", f"━━ **{tag}** · {names[bk]} 공격 ━━", inning, top, outs, bases)
        field_def = T[dk]["field"]
        df = (field_def - F0) / 15

        while outs < 3:
            lead_def = score[dk] - score[bk]
            maybe_change(dk, inning, top, outs, bases, lead_def)
            pitcher = pit[dk]["p"]
            batter = T[bk]["lineup"][order_idx[bk] % 9]
            order_idx[bk] += 1
            b = batter["player_name"]
            pn = pitcher["player_name"]

            # ── 도루 ──
            if bases[0] and not bases[1] and outs < 2:
                runner = next((p for p in T[bk]["lineup"] if p["player_name"] == bases[0]), None)
                if runner and runner["run"] >= 70 and rng.random() < (0.06 + (runner["run"] - 70) / 150) * tn["도루배율"]:
                    ok = 0.62 + (runner["run"] - 70) / 70 - (T[dk]["c_arm"] - A0) / 100
                    if rng.random() < ok:
                        bases[1], bases[0] = bases[0], None
                        add("info", f"💨 {tag} | **{runner['player_name']}** 2루 도루 성공!", inning, top, outs, bases)
                    else:
                        outs += 1
                        add("info", f"🚫 {tag} | **{runner['player_name']}** 도루 실패! 포수의 레이저 송구!",
                            inning, top, outs, [None, bases[1], bases[2]])
                        bases[0] = None
                        if outs >= 3:
                            break

            # ── 긴장 장면 연출 (결과 공개 전 뜸 들이기) ──
            diff = score[bk] - score[dk]
            risp = bases[1] or bases[2]
            buildup = None
            if all(bases):
                buildup = f"🔥 {tag} {outs}사 **만루!!** 타석에는 **{b}**… 마운드의 **{pn}**, 숨을 고릅니다…"
            elif risp and inning >= 7 and abs(diff) <= 2:
                buildup = (f"😰 {tag} {outs}사 {_bases_desc(bases)}, 승부처! **{b}** vs **{pn}**… "
                           + rng.choice(["풀카운트까지 갑니다…!", "파울, 파울… 끈질긴 승부!", "초구 스트라이크… 2구째…"]))
            elif walkoff_possible and diff <= 0 and outs == 2:
                buildup = f"😱 {tag} 2아웃! 경기 끝까지 아웃카운트 하나… 타석에 **{b}**!"
            elif inning >= 9 and diff >= -1 and diff <= 0 and rng.random() < 0.4:
                buildup = f"👀 {tag} {outs}사 {_bases_desc(bases)}, 타석에 **{b}**…"

            # ── 타석 결과 확률 ──
            fatigue = min(20, max(0, pit[dk]["pc"] - 85 - (pitcher["arm"] - A0)) * 0.35)
            arm_eff = pitcher["arm"] - fatigue
            pq = min(3, max(-3, (arm_eff - A0) / 12))
            bonus = 0.25 * form[bk] + (tn["홈어드밴티지"] if bk == 1 else 0)
            dc = min(3, max(-3, (batter["contact"] - C0) / 18 - pq + bonus))
            dp = min(3, max(-3, (batter["power"] - P0) / 18 - pq + bonus))
            probs = {
                "K": 0.20 * math.exp(-0.35 * dc) * tn["삼진배율"],
                "BB": 0.085 * math.exp(-0.2 * pq + 0.08 * dc) * tn["볼넷배율"],
                "HBP": 0.011,
                "HR": 0.019 * math.exp(0.6 * dp) * tn["홈런배율"],
                "3B": 0.004 * math.exp(0.6 * (batter["run"] - R0) / 20) * tn["안타배율"],
                "2B": 0.046 * math.exp(0.3 * dc + 0.2 * dp - 0.2 * df) * tn["안타배율"],
                "1B": 0.150 * math.exp(0.3 * dc - 0.25 * df) * tn["안타배율"],
                "E": 0.012 * math.exp(-0.5 * df),
            }
            probs["OUT"] = max(0.15, 1 - sum(probs.values()))
            res = rng.choices(list(probs), weights=list(probs.values()), k=1)[0]
            pit[dk]["bf"] += 1
            pit[dk]["pc"] += {"K": rng.randint(3, 7), "BB": rng.randint(4, 8), "HBP": rng.randint(1, 5)}.get(
                res, rng.randint(1, 6))

            runs = 0
            hit = res in ("1B", "2B", "3B", "HR")
            fielder = rng.choice(T[dk]["lineup"])["player_name"]
            speed = batter["run"]
            nb = list(bases)
            text = ""

            if res == "HR":
                runs = 1 + sum(1 for x in bases if x)
                nb = [None, None, None]
                if runs == 4:
                    text = f"🔥 {tag} | **{b}** 그랜드슬램!!!!! 만루홈런!!! 🎇🎆"
                else:
                    text = f"💥 {tag} | " + rng.choice(HIT_TEXTS["HR"]).format(b=b) + (f" ({runs}점 홈런)" if runs > 1 else " (솔로)")
            elif res == "3B":
                runs = sum(1 for x in bases if x)
                nb = [None, None, b]
                text = f"🏏 {tag} | " + rng.choice(HIT_TEXTS["3B"]).format(b=b)
            elif res == "2B":
                runs = (1 if bases[2] else 0) + (1 if bases[1] else 0)
                nb = [None, b, None]
                if bases[0]:
                    if rng.random() < 0.42:
                        runs += 1
                    else:
                        nb[2] = bases[0]
                text = f"🏏 {tag} | " + rng.choice(HIT_TEXTS["2B"]).format(b=b)
            elif res == "1B":
                runs = 1 if bases[2] else 0
                nb = [b, None, None]
                if bases[1]:
                    if rng.random() < 0.58:
                        runs += 1
                    else:
                        nb[2] = bases[1]
                if bases[0]:
                    if nb[2] is None and rng.random() < 0.28:
                        nb[2] = bases[0]
                    else:
                        nb[1] = bases[0]
                text = f"🏏 {tag} | " + rng.choice(HIT_TEXTS["1B"]).format(b=b)
            elif res in ("BB", "HBP", "E"):
                if res == "E":
                    # 실책: 모든 주자 한 베이스씩
                    runs = 1 if bases[2] else 0
                    nb = [b, bases[0], bases[1]]
                    text = f"😵 {tag} | **{b}**의 평범한 타구… **{fielder}** 실책!! 타자 주자 살아나갑니다!"
                else:
                    nb = list(bases)
                    if nb[0]:
                        if nb[1]:
                            if nb[2]:
                                runs = 1
                            nb[2] = nb[1]
                        nb[1] = nb[0]
                    nb[0] = b
                    what = "볼넷" if res == "BB" else "몸에 맞는 공"
                    text = (f"🚶 {tag} | **{b}** {what}! 밀어내기로 1점!" if runs
                            else f"🚶 {tag} | **{b}** {what}으로 출루.")
            elif res == "K":
                outs += 1
                text = f"🙅 {tag} | " + rng.choice(OUT_TEXTS["K"]).format(b=b, p=pn)
            else:  # 인플레이 아웃
                kind = rng.choices(["GB", "FB", "LD", "PU"], weights=[45, 35, 10, 10])[0]
                if kind == "GB" and bases[0] and outs < 2 and rng.random() < 0.38 + df * 0.05 - (speed - R0) / 200:
                    outs += 2
                    nb = [None, bases[1], bases[2]]
                    if outs < 3 and bases[2] and rng.random() < 0.5:
                        runs = 1
                        nb[2] = None
                    text = f"⬇️⬇️ {tag} | **{b}** 병살타!! **{fielder}**의 매끄러운 더블플레이!"
                elif kind == "FB" and bases[2] and outs < 2 and rng.random() < 0.6:
                    outs += 1
                    runs = 1
                    nb = [bases[0], bases[1], None]
                    text = f"✈️ {tag} | **{b}** 희생플라이! 3루 주자 **{bases[2]}** 홈인!"
                elif kind == "GB" and outs < 2 and (bases[1] or bases[2]):
                    outs += 1
                    nb = list(bases)
                    if bases[2] and rng.random() < 0.35:
                        runs = 1
                        nb[2] = None
                    if bases[1] and nb[2] is None and rng.random() < 0.6:
                        nb[2], nb[1] = bases[1], None
                    if bases[0] and nb[1] is None:
                        nb[1], nb[0] = bases[0], None
                    text = f"⬇️ {tag} | **{b}** 진루타성 땅볼. 주자들이 한 베이스씩 움직입니다."
                else:
                    outs += 1
                    text = f"🧤 {tag} | " + rng.choice(OUT_TEXTS[kind]).format(b=b, f=fielder)

            # 끝내기: 홈런이 아니면 결승 득점까지만 인정
            walkoff = False
            if walkoff_possible and score[1] + runs > score[0]:
                if res != "HR":
                    runs = score[0] - score[1] + 1
                walkoff = True

            before = (score[bk], score[dk])
            score[bk] += runs
            pit[dk]["ra"] += runs
            bases = nb
            if outs >= 3:
                bases = [None, None, None]

            alert = None
            if runs:
                text += f"  〔{names[0]} {score[0]} : {score[1]} {names[1]}〕"
                flipped = before[0] <= before[1] and score[bk] > score[dk]
                tied = before[0] < before[1] and score[bk] == score[dk]
                if walkoff:
                    alert = (f"🎉🎉🎉 **끝내기!!!** {tag} **{b}**, **{names[bk]}**(이)가 경기를 끝냅니다!!\n"
                             f"**{names[0]} {score[0]} : {score[1]} {names[1]}**")
                elif res == "HR":
                    alert = (f"💥 **{'만루 ' if runs == 4 else ''}홈런!!** {tag} **{b}** ({names[bk]})"
                             f"{' — 역전!!' if flipped else ' — 동점!!' if tied else ''}\n"
                             f"**{names[0]} {score[0]} : {score[1]} {names[1]}**")
                elif flipped and inning >= 6:
                    alert = f"🔄 **역전!!** {tag} **{b}** ({names[bk]})\n**{names[0]} {score[0]} : {score[1]} {names[1]}**"
                elif tied and inning >= 7:
                    alert = f"⚖️ **동점!!** {tag} **{b}** ({names[bk]})\n**{names[0]} {score[0]} : {score[1]} {names[1]}**"
            if res == "HR" and buildup is None:
                buildup = f"⚡ {tag} | **{b}**의 타구가 높이… 멀리…!!"
            if walkoff and buildup is None:
                buildup = f"⚡ {tag} | **{b}**, 받아칩니다…!!"

            add("pa", text, inning, top, outs, bases, runs=runs, hit=hit, batter=b, pitcher=pn,
                pitches=pit[dk]["pc"], res=res, buildup=buildup, alert=alert)
            if walkoff:
                return True
        return False

    innings = 0
    for inning in range(1, MAX_INNINGS + 1):
        innings = inning
        if inning == 10:
            add("info", "⏰ **연장전 돌입!!** 한 점 싸움입니다!", inning, True, 0, [None] * 3,
                alert=f"⏰ **연장 돌입!** {names[0]} {score[0]} : {score[1]} {names[1]}")
        half_inning(inning, True)
        if inning >= 9 and score[1] > score[0]:
            add("info", f"🏁 {inning}회말 공격 없이 경기 종료 — **{home}**(홈) 리드 유지!", inning, False, 0, [None] * 3)
            break
        if half_inning(inning, False):
            break
        if inning >= 9 and score[0] != score[1]:
            break

    if score[0] > score[1]:
        res = f"🏆 **{away}** 승리!"
    elif score[1] > score[0]:
        res = f"🏆 **{home}** 승리!"
    else:
        res = f"🤝 {MAX_INNINGS}회까지 승부를 가리지 못해 무승부!"
    add("end", f"🏁 **경기 종료!!** {away} {score[0]} : {score[1]} {home} — {res}", innings, False, 3, [None] * 3)
    return BaseballMatch(away, home, events, tuple(score), innings, {"away": T[0], "home": T[1]})


# ═══════════════════════════════════════════════════════════════
#  승부 예측 (몬테카를로)
# ═══════════════════════════════════════════════════════════════
def win_odds(sport, first, second, first_players, second_players, n=200, **kw):
    """(첫째 팀 승, 무, 둘째 팀 승) 확률 — 축구는 (홈, 원정), 야구는 (원정, 홈) 순서. kw는 시뮬레이터에 전달"""
    rng = random.Random()
    w = d = l = 0
    for _ in range(n):
        if sport == "축구":
            a, b = simulate_soccer(first, second, first_players, second_players, rng, **kw).score
        else:
            a, b = simulate_baseball(first, second, first_players, second_players, rng, **kw).score
        if a > b:
            w += 1
        elif a < b:
            l += 1
        else:
            d += 1
    return w / n, d / n, l / n
