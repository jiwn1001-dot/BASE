"""
가상 국가 디스코드 봇 — 메인 엔트리포인트
정치 · 경제 · RP · 스포츠 · 전쟁 시뮬레이션 통합 봇
"""

import discord
from discord.ext import commands
import aiosqlite
import os
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# ─── 경로 설정 ───────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "nation.db")

# ─── 인텐트 ──────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ═══════════════════════════════════════════════════════════════
#  데이터베이스 초기화
# ═══════════════════════════════════════════════════════════════
async def setup_db():
    """모든 테이블을 생성하고 기본 행을 삽입한다."""
    async with aiosqlite.connect(DB_PATH) as db:
        # ── 1. server_settings ────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS server_settings (
                id                  INTEGER PRIMARY KEY,
                cumulative_inflation REAL    DEFAULT 1.0,
                soccer_channel_id   INTEGER,
                baseball_channel_id INTEGER
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO server_settings (id, cumulative_inflation) VALUES (1, 1.0)"
        )

        # ── 2. users ─────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                rp_name     TEXT,
                profile_url TEXT,
                money       INTEGER DEFAULT 0,
                salary      INTEGER DEFAULT 0,
                str         INTEGER DEFAULT 0,
                spd         INTEGER DEFAULT 0,
                int         INTEGER DEFAULT 0
            )
        """)

        # ── 3. stocks ────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                company_name   TEXT    PRIMARY KEY,
                owner_id       INTEGER,
                sector_name    TEXT,
                current_price  INTEGER,
                total_shares   INTEGER,
                economic_phase INTEGER DEFAULT 3
            )
        """)

        # ── 4. items ─────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                item_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name  TEXT,
                base_price INTEGER,
                str_bonus  INTEGER DEFAULT 0,
                spd_bonus  INTEGER DEFAULT 0,
                int_bonus  INTEGER DEFAULT 0
            )
        """)

        # ── 5. inventory ─────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                user_id    INTEGER,
                item_id    INTEGER,
                is_equipped BOOLEAN DEFAULT FALSE
            )
        """)

        # ── 6. parties ───────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS parties (
                party_name   TEXT PRIMARY KEY,
                rep_user_id  INTEGER,
                house_seats  INTEGER,
                senate_seats INTEGER,
                approval_rate REAL
            )
        """)

        # ── 7. sports_teams ──────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sports_teams (
                team_name  TEXT    PRIMARY KEY,
                owner_id   INTEGER,
                sport_type TEXT,
                wins       INTEGER DEFAULT 0,
                draws      INTEGER DEFAULT 0,
                losses     INTEGER DEFAULT 0,
                points     INTEGER DEFAULT 0
            )
        """)

        # ── 8. baseball_players ──────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS baseball_players (
                player_name       TEXT    PRIMARY KEY,
                team_name         TEXT    DEFAULT '무소속',
                base_transfer_fee INTEGER,
                contact           INTEGER,
                power             INTEGER,
                run               INTEGER,
                arm               INTEGER,
                field             INTEGER
            )
        """)

        # ── 9. soccer_players ────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS soccer_players (
                player_name       TEXT    PRIMARY KEY,
                team_name         TEXT    DEFAULT '무소속',
                base_transfer_fee INTEGER,
                pace              INTEGER,
                shooting          INTEGER,
                passing           INTEGER,
                dribbling         INTEGER,
                defending         INTEGER,
                physical          INTEGER
            )
        """)

        # ── 10. nations ──────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nations (
                region_name TEXT PRIMARY KEY,
                chrome      INTEGER,
                tungsten    INTEGER,
                saltpeter   INTEGER,
                steel       INTEGER,
                aluminum    INTEGER,
                oil         INTEGER,
                rubber      INTEGER,
                food        INTEGER
            )
        """)

        # ── 보조 테이블: stock_holdings (주식 보유) ──────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_holdings (
                user_id      INTEGER,
                company_name TEXT,
                quantity     INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, company_name)
            )
        """)

        # ── 보조 테이블: team_creation_permissions ───────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS team_creation_permissions (
                user_id    INTEGER,
                sport_type TEXT,
                PRIMARY KEY (user_id, sport_type)
            )
        """)

        await db.commit()
    print("[DB] 데이터베이스 초기화 완료")


# ═══════════════════════════════════════════════════════════════
#  유틸리티
# ═══════════════════════════════════════════════════════════════
async def ensure_user(user_id: int):
    """유저가 DB에 없으면 기본값으로 삽입"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()


# 봇 어트리뷰트에 공유 자원 등록
bot.db_path = DB_PATH
bot.ensure_user = ensure_user


# ═══════════════════════════════════════════════════════════════
#  on_ready — DB 세팅 + Cog 로드 + 트리 동기화
# ═══════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    await setup_db()

    cogs_dir = os.path.join(BASE_DIR, "cogs")
    for filename in os.listdir(cogs_dir):
        if filename.endswith(".py") and not filename.startswith("_"):
            ext_name = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(ext_name)
                print(f"[COG] {ext_name} 로드 완료")
            except Exception as e:
                print(f"[COG] {ext_name} 로드 실패: {e}")

    try:
        synced = await bot.tree.sync()
        print(f"[SYNC] {len(synced)}개 슬래시 커맨드 동기화 완료")
    except Exception as e:
        print(f"[SYNC] 커맨드 동기화 실패: {e}")

    print(f"[BOT] {bot.user} 가상 국가 봇 — 가동 준비 완료!")


# ═══════════════════════════════════════════════════════════════
#  실행
# ═══════════════════════════════════════════════════════════════
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
bot.run(TOKEN)
