"""
cogs/_config.py — 관리자가 실시간으로 바꿀 수 있는 게임 수치 모음
DB(game_config 테이블)에 저장되고 메모리에 캐시된다.
/설정보기 · /설정변경 · /설정초기화 (cogs/admin_config.py) 로 조절.
(파일명이 _ 로 시작하므로 Cog 자동 로드 대상 아님)
"""

import aiosqlite

# 키: (기본값, 최소, 최대, 설명)
DEFAULTS: dict[str, tuple[float, float, float, str]] = {
    # ── 주식 시장 ──────────────────────────────────────────
    "주식_틱간격분": (10, 1, 60, "시세가 움직이는 주기(분)"),
    "주식_평균회귀": (0.03, 0, 0.5, "틱마다 적정가로 되돌아가는 강도 (0=순수 랜덤워크)"),
    "주식_변동성배율": (1.0, 0, 10, "모든 기업 변동성에 곱해지는 배율"),
    "주식_시장연동": (0.35, 0, 0.9, "시장 전체와 같이 움직이는 비중"),
    "주식_섹터연동": (0.35, 0, 0.9, "같은 섹터끼리 같이 움직이는 비중"),
    "주식_모멘텀": (0.08, 0, 0.5, "직전 흐름을 따라가는 추세 강도"),
    "주식_가격제한폭": (0.30, 0.05, 1.0, "하루 상·하한가 폭 (0.30 = ±30%)"),
    "주식_거래충격": (0.5, 0, 5, "발행주식 1% 거래 시 가격이 움직이는 %"),
    "주식_수수료": (0.0025, 0, 0.1, "매수·매도 수수료율 (0.0025 = 0.25%)"),
    "주식_뉴스빈도": (0.8, 0, 20, "기업당 하루 평균 뉴스 횟수"),
    "주식_뉴스강도": (1.0, 0, 5, "뉴스 주가 영향 배율"),
    "주식_시장이벤트빈도": (0.4, 0, 20, "하루 평균 시장/섹터 전체 이벤트 횟수"),
    "주식_배당률": (0.0, 0, 5, "매일 00시 보유 주식 평가액 대비 배당(%)"),
    "주식_대주주지분": (0.5, 0, 1, "신규 상장 시 대주주 지분 (나머지는 시장 유통 물량)"),
    "주식_단계1_수익률": (-1.2, -50, 50, "불황 단계 일간 기대수익률(%)"),
    "주식_단계1_변동성": (2.8, 0, 50, "불황 단계 일간 변동성(%)"),
    "주식_단계2_수익률": (1.0, -50, 50, "회복기 단계 일간 기대수익률(%)"),
    "주식_단계2_변동성": (2.2, 0, 50, "회복기 단계 일간 변동성(%)"),
    "주식_단계3_수익률": (0.15, -50, 50, "보통 단계 일간 기대수익률(%)"),
    "주식_단계3_변동성": (1.6, 0, 50, "보통 단계 일간 변동성(%)"),
    "주식_단계4_수익률": (1.8, -50, 50, "경기과열 단계 일간 기대수익률(%)"),
    "주식_단계4_변동성": (4.0, 0, 50, "경기과열 단계 일간 변동성(%)"),
    "주식_단계5_수익률": (-3.5, -50, 50, "공황 단계 일간 기대수익률(%)"),
    "주식_단계5_변동성": (5.5, 0, 50, "공황 단계 일간 변동성(%)"),
    # ── 경기 일정 · 중계 ──────────────────────────────────
    "경기_중계시간초": (300, 60, 1800, "경기 1개 실시간 중계 길이(초) — /시즌시작 후 적용"),
    "경기_최소휴식초": (120, 0, 3600, "경기 사이 최소 휴식(초) — /시즌시작 후 적용"),
    "경기_목표휴식초": (180, 0, 7200, "경기 사이 목표 휴식(초), 팀 수에 맞춰 경기 수 자동 조절"),
    "경기_뜸들이기초": (2.5, 0, 10, "결정적 장면 결과 공개 전 대기(초)"),
    "경기_폼영향": (1.0, 0, 5, "최근 5경기 성적(기세)이 경기력에 주는 영향 배율"),
    # ── 축구 ──────────────────────────────────────────────
    "축구_슈팅배율": (1.0, 0.1, 5, "슈팅 빈도 배율"),
    "축구_득점배율": (1.0, 0.1, 5, "슈팅당 득점 확률(xG) 배율"),
    "축구_홈어드밴티지": (0.03, 0, 0.3, "홈팀 점유율 보너스"),
    "축구_카드배율": (1.0, 0, 5, "경고·퇴장 빈도 배율"),
    "축구_PK배율": (1.0, 0, 5, "페널티킥 빈도 배율"),
    "축구_흐름변화": (1.0, 0, 5, "주도권(모멘텀) 변화 빈도 배율"),
    "축구_최대선수": (25, 11, 60, "구단당 최대 보유 선수 수"),
    # ── 야구 ──────────────────────────────────────────────
    "야구_안타배율": (1.0, 0.1, 5, "안타 확률 배율"),
    "야구_홈런배율": (1.0, 0.1, 5, "홈런 확률 배율"),
    "야구_삼진배율": (1.0, 0.1, 5, "삼진 확률 배율"),
    "야구_볼넷배율": (1.0, 0.1, 5, "볼넷 확률 배율"),
    "야구_도루배율": (1.0, 0, 5, "도루 시도 빈도 배율"),
    "야구_홈어드밴티지": (0.1, 0, 1, "홈 타자 타격 보너스"),
    "야구_최대선수": (28, 9, 60, "구단당 최대 보유 선수 수"),
}

CATEGORIES = {"주식": "📈", "경기": "📺", "축구": "⚽", "야구": "⚾"}

_cache: dict[str, float] = {}


def get(key: str) -> float:
    return _cache.get(key, DEFAULTS[key][0])


def tune(prefix: str) -> dict[str, float]:
    """'축구' → {'슈팅배율': 1.0, ...}"""
    return {k.split("_", 1)[1]: get(k) for k in DEFAULTS if k.startswith(prefix + "_")}


async def load(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS game_config (key TEXT PRIMARY KEY, value REAL)")
        await db.commit()
        cur = await db.execute("SELECT key, value FROM game_config")
        for k, v in await cur.fetchall():
            if k in DEFAULTS:
                _cache[k] = v


async def set_value(db_path: str, key: str, value: float) -> float:
    _, lo, hi, _ = DEFAULTS[key]
    value = min(max(value, lo), hi)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS game_config (key TEXT PRIMARY KEY, value REAL)")
        await db.execute("INSERT OR REPLACE INTO game_config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    _cache[key] = value
    return value


async def reset(db_path: str, key: str | None = None):
    async with aiosqlite.connect(db_path) as db:
        if key:
            await db.execute("DELETE FROM game_config WHERE key = ?", (key,))
            _cache.pop(key, None)
        else:
            await db.execute("DELETE FROM game_config")
            _cache.clear()
        await db.commit()


def fmt(v: float) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:g}"
