"""
cogs/sports.py — 스포츠 시뮬레이션 시스템
K리그(축구) · KBO(야구) 구단 관리 · 선수 이적 · 24시간 실시간 중계

· 시즌 = 하루 (00시 턴 넘기기 → 다음 00시)
· 등록된 팀 수에 맞춰 경기 수와 경기 간격을 자동 계산해 24시간을 채움
· 경기 1개 = 5분 실시간 중계 (전광판 임베드가 계속 갱신 + 골/홈런 속보)
"""

import asyncio
import random
import math
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

from ._match_engine import simulate_soccer, simulate_baseball, win_odds, baseball_team_profile
from . import _config as cfg

KST = timezone(timedelta(hours=9))

# ─── 팀 수 제한 ──────────────────────────────────────────────
MAX_SOCCER_TEAMS = 12
MAX_BASEBALL_TEAMS = 10

# ─── 중계/일정 설정 ─────────────────────────────────────────
# 경기 길이·휴식·뜸 들이기는 /설정변경 으로 조절 (경기_중계시간초 등)
PREVIEW_LEAD = 60            # 킥오프 60초 전 프리뷰
EDIT_INTERVAL = 6            # 이벤트가 없을 때 전광판(시계) 갱신 주기
MIN_EDIT_GAP = 2.0           # 연속 수정 최소 간격 (디스코드 레이트리밋 보호)
LOG_LINES = 9                # 전광판에 보여줄 문자중계 줄 수
FORM_GAMES = 5               # 기세 계산에 쓰는 최근 경기 수


def match_seconds():
    return cfg.get("경기_중계시간초")


def suspense():
    return cfg.get("경기_뜸들이기초")

SPORT_META = {
    "축구": {"emoji": "⚽", "league": "K리그"},
    "야구": {"emoji": "⚾", "league": "KBO"},
}


@dataclass
class Fixture:
    sport: str
    home: str
    away: str
    round: int
    kickoff: float           # epoch seconds


def round_robin(teams):
    """서클 방식 라운드로빈 — 한 라운드에 모든 팀이 한 경기씩 (홀수면 한 팀 휴식)"""
    ts = list(teams)
    if len(ts) % 2:
        ts.append(None)
    n = len(ts)
    rounds = []
    for r in range(n - 1):
        pairs = []
        for i in range(n // 2):
            a, b = ts[i], ts[n - 1 - i]
            if a is None or b is None:
                continue
            pairs.append((a, b) if (r + i) % 2 == 0 else (b, a))
        rounds.append(pairs)
        ts = [ts[0], ts[-1]] + ts[1:-1]
    return rounds


def build_schedule(sport, teams, start, end):
    """
    [start, end) 구간을 경기로 채운다.
    팀이 많으면 라운드로빈 1바퀴가 하루를 넘지 않게 자르고,
    팀이 적으면 라운드로빈을 여러 바퀴(홈/원정 교대) 돌려 경기 간격을 목표 휴식 근처로 맞춘다.
    킥오프는 구간 전체에 균등 분배 → 24시간 내내 쉬지 않고 경기가 이어짐.
    """
    n = len(teams)
    window = end - start
    if n < 2 or window <= 0:
        return []
    max_games = int(window // (match_seconds() + cfg.get("경기_최소휴식초")))
    if max_games < 1:
        return []
    base = round_robin(random.sample(teams, n))
    per_cycle = sum(len(r) for r in base)
    target = max(1, int(window // (match_seconds() + max(cfg.get("경기_목표휴식초"), cfg.get("경기_최소휴식초")))))
    cycles = max(1, round(target / per_cycle))
    while cycles > 1 and cycles * per_cycle > max_games:
        cycles -= 1

    games = []
    rnd = 0
    for c in range(cycles):
        for pairs in base:
            rnd += 1
            rp = [(b, a) if c % 2 else (a, b) for a, b in pairs]
            random.shuffle(rp)
            games += [(rnd, h, a) for h, a in rp]
    games = games[:max_games]
    slot = window / len(games)
    return [Fixture(sport, h, a, r, start + i * slot) for i, (r, h, a) in enumerate(games)]


def next_midnight_kst(now_ts: float) -> float:
    now = datetime.fromtimestamp(now_ts, KST)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _dw(s):
    """고정폭 표시 너비 (한글 2칸)"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, width):
    out = ""
    for c in s:
        if _dw(out + c) > width:
            break
        out += c
    return out + " " * (width - _dw(out))


def _trim_log(lines, limit=1000):
    lines = list(lines)
    while lines and len("\n".join(lines)) > limit:
        lines.pop(0)
    return "\n".join(lines) or "​"


def _odds_bar(p1, pd, p2, n1, n2, sport):
    if sport == "축구":
        return f"**{n1}** {p1 * 100:.0f}% · 무 {pd * 100:.0f}% · **{n2}** {p2 * 100:.0f}%"
    return f"**{n1}** {p1 * 100:.0f}% · **{n2}** {p2 * 100:.0f}%"


class SportsCog(commands.Cog):
    """스포츠(축구/야구) 시뮬레이션 관리 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.schedule: dict[str, list[Fixture]] = {"축구": [], "야구": []}
        self.runners: dict[str, asyncio.Task] = {}
        self.live: dict[str, Fixture | None] = {"축구": None, "야구": None}
        self._boot_task: asyncio.Task | None = None
        self.form: dict[str, list[str]] = {}      # 팀별 최근 결과 ['W','D','L', ...]
        self.rotation: dict[str, int] = {}        # 야구 팀별 다음 선발 순번

    async def cog_load(self):
        await cfg.load(self.db)
        self._boot_task = asyncio.create_task(self._boot())

    async def _boot(self):
        await self.bot.wait_until_ready()
        await self.start_season()

    async def cog_unload(self):
        if self._boot_task:
            self._boot_task.cancel()
        await self.stop_runners()

    @property
    def db(self):
        return self.bot.db_path

    @property
    def season_active(self):
        return any(not t.done() for t in self.runners.values())

    # ═════════════════════════════════════════════════════════
    #  채널 설정
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="축구채널설정", description="축구 중계 채널을 설정합니다")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_soccer_channel(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE server_settings SET soccer_channel_id = ? WHERE id = 1",
                (interaction.channel_id,),
            )
            await db.commit()
        await interaction.response.send_message(
            f"⚽ 축구 중계 채널이 {interaction.channel.mention}(으)로 설정되었습니다!"
        )

    @app_commands.command(name="야구채널설정", description="야구 중계 채널을 설정합니다")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_baseball_channel(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE server_settings SET baseball_channel_id = ? WHERE id = 1",
                (interaction.channel_id,),
            )
            await db.commit()
        await interaction.response.send_message(
            f"⚾ 야구 중계 채널이 {interaction.channel.mention}(으)로 설정되었습니다!"
        )

    # ═════════════════════════════════════════════════════════
    #  구단 창설 권한
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="구단창설권한", description="유저에게 구단 창설 권한을 부여합니다 (관리자)")
    @app_commands.describe(유저="대상 유저", 종목="축구 또는 야구")
    @app_commands.checks.has_permissions(administrator=True)
    async def grant_team_perm(
        self, interaction: discord.Interaction, 유저: discord.Member, 종목: str
    ):
        if 종목 not in ("축구", "야구"):
            return await interaction.response.send_message(
                "❌ 종목은 `축구` 또는 `야구`여야 합니다.", ephemeral=True
            )
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "INSERT OR IGNORE INTO team_creation_permissions (user_id, sport_type) VALUES (?, ?)",
                (유저.id, 종목),
            )
            await db.commit()
        await interaction.response.send_message(
            f"✅ {유저.mention}에게 **{종목}** 구단 창설 권한이 부여되었습니다!"
        )

    # ═════════════════════════════════════════════════════════
    #  구단 창설
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="구단창설", description="스포츠 구단을 창설합니다")
    @app_commands.describe(구단명="구단 이름", 종목="축구 또는 야구")
    async def create_team(
        self, interaction: discord.Interaction, 구단명: str, 종목: str
    ):
        uid = interaction.user.id
        if 종목 not in ("축구", "야구"):
            return await interaction.response.send_message(
                "❌ 종목은 `축구` 또는 `야구`여야 합니다.", ephemeral=True
            )

        async with aiosqlite.connect(self.db) as db:
            # 권한 확인
            cur = await db.execute(
                "SELECT user_id FROM team_creation_permissions WHERE user_id = ? AND sport_type = ?",
                (uid, 종목),
            )
            if not await cur.fetchone():
                return await interaction.response.send_message(
                    "❌ 구단 창설 권한이 없습니다. 관리자에게 `/구단창설권한`을 요청하세요.",
                    ephemeral=True,
                )

            # 팀 수 제한 확인
            max_teams = MAX_SOCCER_TEAMS if 종목 == "축구" else MAX_BASEBALL_TEAMS
            league_name = "K리그" if 종목 == "축구" else "KBO"
            cur = await db.execute(
                "SELECT COUNT(*) FROM sports_teams WHERE sport_type = ?", (종목,)
            )
            current_count = (await cur.fetchone())[0]
            if current_count >= max_teams:
                return await interaction.response.send_message(
                    f"❌ {league_name}는 최대 {max_teams}개 팀으로 제한됩니다. "
                    f"(현재 {current_count}개)",
                    ephemeral=True,
                )

            # 중복 확인
            cur = await db.execute(
                "SELECT team_name FROM sports_teams WHERE team_name = ?", (구단명,)
            )
            if await cur.fetchone():
                return await interaction.response.send_message(
                    "❌ 이미 존재하는 구단명입니다.", ephemeral=True
                )

            await db.execute(
                """INSERT INTO sports_teams
                   (team_name, owner_id, sport_type, wins, draws, losses, points)
                   VALUES (?, ?, ?, 0, 0, 0, 0)""",
                (구단명, uid, 종목),
            )
            # 권한 소비
            await db.execute(
                "DELETE FROM team_creation_permissions WHERE user_id = ? AND sport_type = ?",
                (uid, 종목),
            )
            await db.commit()

        emoji = "⚽" if 종목 == "축구" else "⚾"
        embed = discord.Embed(
            title=f"{emoji} 구단 창설 완료!",
            description=f"**{구단명}**이(가) {league_name}에 등록되었습니다!",
            colour=0x2ECC71,
        )
        embed.add_field(name="구단주", value=interaction.user.mention, inline=True)
        embed.add_field(name="종목", value=종목, inline=True)
        embed.add_field(
            name="리그 현황", value=f"{current_count + 1}/{max_teams}팀", inline=True
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  구단 이름 수정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="구단이름수정", description="스포츠 구단의 이름을 변경합니다 (관리자)")
    @app_commands.describe(기존구단명="변경할 기존 구단 이름", 새구단명="새로운 구단 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def rename_team(self, interaction: discord.Interaction, 기존구단명: str, 새구단명: str):
        if 기존구단명 == "무소속" or 새구단명 == "무소속":
            return await interaction.response.send_message("❌ '무소속' 이라는 이름은 사용할 수 없습니다.", ephemeral=True)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT sport_type FROM sports_teams WHERE team_name = ?", (기존구단명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 기존 구단이 존재하지 않습니다.", ephemeral=True)

            cur = await db.execute("SELECT team_name FROM sports_teams WHERE team_name = ?", (새구단명,))
            if await cur.fetchone():
                return await interaction.response.send_message("❌ 새 구단 이름이 이미 존재합니다.", ephemeral=True)

            # 구단 이름 변경
            await db.execute("UPDATE sports_teams SET team_name = ? WHERE team_name = ?", (새구단명, 기존구단명))
            
            # 소속 선수들의 팀명도 함께 변경
            await db.execute("UPDATE soccer_players SET team_name = ? WHERE team_name = ?", (새구단명, 기존구단명))
            await db.execute("UPDATE baseball_players SET team_name = ? WHERE team_name = ?", (새구단명, 기존구단명))
            await db.commit()

        # 오늘 남은 경기 일정에도 새 이름 반영
        for fx in self.schedule.get(row[0], []):
            if fx.home == 기존구단명:
                fx.home = 새구단명
            if fx.away == 기존구단명:
                fx.away = 새구단명

        embed = discord.Embed(
            title="✏️ 구단 이름 변경 완료",
            description=f"구단 이름이 **{기존구단명}**에서 **{새구단명}**(으)로 성공적으로 변경되었습니다.\n소속 선수들의 정보도 함께 업데이트되었습니다.",
            colour=0x3498DB,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  구단 삭제 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="구단삭제", description="스포츠 구단을 삭제합니다 (관리자)")
    @app_commands.describe(구단명="삭제할 구단 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_team(self, interaction: discord.Interaction, 구단명: str):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT sport_type FROM sports_teams WHERE team_name = ?", (구단명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 해당 구단이 존재하지 않습니다.", ephemeral=True)
            
            # 구단 삭제
            await db.execute("DELETE FROM sports_teams WHERE team_name = ?", (구단명,))
            
            # 소속 선수들 무소속(FA) 처리
            await db.execute("UPDATE soccer_players SET team_name = '무소속' WHERE team_name = ?", (구단명,))
            await db.execute("UPDATE baseball_players SET team_name = '무소속' WHERE team_name = ?", (구단명,))
            await db.commit()

        embed = discord.Embed(
            title="🗑️ 구단 해체 완료",
            description=f"**{구단명}** 구단이 해체되었습니다. 소속 선수들은 모두 무소속(FA)으로 전환됩니다.",
            colour=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  시즌 강제 시작 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="시즌시작", description="오늘 남은 시간에 맞춰 경기 일정을 다시 짜고 중계를 시작합니다 (관리자)")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_start_season(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.start_season()
        total = sum(len(v) for v in self.schedule.values())

        if total == 0:
            return await interaction.followup.send(
                "❌ 경기를 생성할 수 없습니다. (종목당 구단 2개 이상 필요, 또는 자정까지 남은 시간이 너무 짧음)"
            )

        embed = discord.Embed(
            title="🏁 시즌 일정 편성 완료",
            description=f"자정까지 **총 {total}경기**가 편성되었습니다. 경기마다 5분간 실시간 중계됩니다!",
            colour=0x2ECC71,
        )
        for sport, fxs in self.schedule.items():
            embed.add_field(name=f"{SPORT_META[sport]['emoji']} {SPORT_META[sport]['league']}",
                            value=self._schedule_summary(fxs), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="경기일정", description="진행 중인 경기와 다음 경기 일정을 확인합니다")
    @app_commands.describe(종목="축구 또는 야구")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구"),
    ])
    async def show_schedule(self, interaction: discord.Interaction, 종목: app_commands.Choice[str]):
        sport = 종목.value
        meta = SPORT_META[sport]
        now = time.time()
        upcoming = [f for f in self.schedule[sport] if f.kickoff > now][:10]
        live = self.live[sport]
        lines = []
        if live:
            lines.append(f"🔴 **LIVE** {live.round}R · **{live.home}** vs **{live.away}**")
        for f in upcoming:
            t = int(f.kickoff)
            lines.append(f"<t:{t}:t> (<t:{t}:R>) · {f.round}R · **{f.home}** vs **{f.away}**")
        embed = discord.Embed(
            title=f"{meta['emoji']} {meta['league']} 오늘의 일정",
            description="\n".join(lines) or "남은 경기가 없습니다.",
            colour=0x3498DB,
        )
        embed.set_footer(text=self._schedule_summary(self.schedule[sport]).replace("**", ""))
        await interaction.response.send_message(embed=embed)

    def _schedule_summary(self, fxs):
        if not fxs:
            return "편성된 경기 없음 (구단 2개 이상 필요)"
        gap = (fxs[1].kickoff - fxs[0].kickoff) if len(fxs) > 1 else match_seconds()
        remaining = sum(1 for f in fxs if f.kickoff > time.time())
        return (f"**{len(fxs)}경기** · {fxs[-1].round}라운드 · 약 **{gap / 60:.1f}분**마다 킥오프 "
                f"(남은 경기 {remaining})")

    # ═════════════════════════════════════════════════════════
    #  선수 등록 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="선수등록", description="무소속 선수를 등록합니다 (관리자)")
    @app_commands.describe(
        종목="축구 또는 야구",
        이름="선수 이름",
        기본이적료="기본 이적료",
        스탯1="축구: pace / 야구: contact",
        스탯2="축구: shooting / 야구: power",
        스탯3="축구: passing / 야구: run",
        스탯4="축구: dribbling / 야구: arm",
        스탯5="축구: defending / 야구: field",
        스탯6="축구 전용: physical (야구는 무시됨)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def register_player(
        self,
        interaction: discord.Interaction,
        종목: str,
        이름: str,
        기본이적료: int,
        스탯1: int,
        스탯2: int,
        스탯3: int,
        스탯4: int,
        스탯5: int,
        스탯6: int = 0,
    ):
        if 종목 not in ("축구", "야구"):
            return await interaction.response.send_message(
                "❌ 종목은 `축구` 또는 `야구`여야 합니다.", ephemeral=True
            )

        async with aiosqlite.connect(self.db) as db:
            if 종목 == "축구":
                cur = await db.execute(
                    "SELECT player_name FROM soccer_players WHERE player_name = ?",
                    (이름,),
                )
                if await cur.fetchone():
                    return await interaction.response.send_message(
                        "❌ 이미 존재하는 선수입니다.", ephemeral=True
                    )
                await db.execute(
                    """INSERT INTO soccer_players
                       (player_name, team_name, base_transfer_fee,
                        pace, shooting, passing, dribbling, defending, physical)
                       VALUES (?, '무소속', ?, ?, ?, ?, ?, ?, ?)""",
                    (이름, 기본이적료, 스탯1, 스탯2, 스탯3, 스탯4, 스탯5, 스탯6),
                )
                stat_text = (
                    f"🏃 pace: {스탯1} | 🎯 shooting: {스탯2} | "
                    f"📐 passing: {스탯3} | 🌀 dribbling: {스탯4} | "
                    f"🛡️ defending: {스탯5} | 💪 physical: {스탯6}"
                )
            else:
                cur = await db.execute(
                    "SELECT player_name FROM baseball_players WHERE player_name = ?",
                    (이름,),
                )
                if await cur.fetchone():
                    return await interaction.response.send_message(
                        "❌ 이미 존재하는 선수입니다.", ephemeral=True
                    )
                await db.execute(
                    """INSERT INTO baseball_players
                       (player_name, team_name, base_transfer_fee,
                        contact, power, run, arm, field)
                       VALUES (?, '무소속', ?, ?, ?, ?, ?, ?)""",
                    (이름, 기본이적료, 스탯1, 스탯2, 스탯3, 스탯4, 스탯5),
                )
                stat_text = (
                    f"👆 contact: {스탯1} | 💥 power: {스탯2} | "
                    f"🏃 run: {스탯3} | 💪 arm: {스탯4} | "
                    f"🧤 field: {스탯5}"
                )
            await db.commit()

        emoji = "⚽" if 종목 == "축구" else "⚾"
        embed = discord.Embed(
            title=f"{emoji} 선수 등록 완료",
            description=f"**{이름}** (무소속)",
            colour=0x9B59B6,
        )
        embed.add_field(name="기본 이적료", value=f"{기본이적료:,}원", inline=True)
        embed.add_field(name="스탯", value=stat_text, inline=False)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  이적시장 구매 (물가 반영)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="이적시장구매", description="이적 시장에서 선수를 영입합니다")
    @app_commands.describe(선수명="영입할 선수 이름")
    async def transfer_buy(self, interaction: discord.Interaction, 선수명: str):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT cumulative_inflation FROM server_settings WHERE id = 1"
            )
            inflation = (await cur.fetchone())["cumulative_inflation"]

            # 축구에서 먼저 검색
            cur = await db.execute(
                "SELECT player_name, team_name, base_transfer_fee FROM soccer_players WHERE player_name = ?",
                (선수명,),
            )
            player = await cur.fetchone()
            sport = "축구"

            if not player:
                cur = await db.execute(
                    "SELECT player_name, team_name, base_transfer_fee FROM baseball_players WHERE player_name = ?",
                    (선수명,),
                )
                player = await cur.fetchone()
                sport = "야구"

            if not player:
                return await interaction.response.send_message(
                    "❌ 해당 선수가 존재하지 않습니다.", ephemeral=True
                )

            if player["team_name"] != "무소속":
                return await interaction.response.send_message(
                    f"❌ **{선수명}**은(는) 이미 **{player['team_name']}** 소속입니다.",
                    ephemeral=True,
                )

            # 유저 소유 팀 확인
            cur = await db.execute(
                "SELECT team_name FROM sports_teams WHERE owner_id = ? AND sport_type = ?",
                (uid, sport),
            )
            team_row = await cur.fetchone()
            if not team_row:
                return await interaction.response.send_message(
                    f"❌ 소유한 {sport} 구단이 없습니다.", ephemeral=True
                )

            team_name = team_row["team_name"]
            table = "soccer_players" if sport == "축구" else "baseball_players"
            cap = int(cfg.get(f"{sport}_최대선수"))
            cur = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE team_name = ?", (team_name,))
            roster = (await cur.fetchone())[0]
            if roster >= cap:
                return await interaction.response.send_message(
                    f"❌ **{team_name}** 선수단이 가득 찼습니다 ({roster}/{cap}명). "
                    f"선수를 방출하거나 관리자에게 문의하세요.", ephemeral=True)
            transfer_fee = int(player["base_transfer_fee"] * inflation)

            cur = await db.execute(
                "SELECT money FROM users WHERE user_id = ?", (uid,)
            )
            money = (await cur.fetchone())["money"]
            if money < transfer_fee:
                return await interaction.response.send_message(
                    f"❌ 이적료 부족! 필요: {transfer_fee:,}원 / 보유: {money:,}원",
                    ephemeral=True,
                )

            await db.execute(
                "UPDATE users SET money = money - ? WHERE user_id = ?",
                (transfer_fee, uid),
            )

            await db.execute(
                f"UPDATE {table} SET team_name = ? WHERE player_name = ?",
                (team_name, 선수명),
            )
            await db.commit()

        emoji = "⚽" if sport == "축구" else "⚾"
        embed = discord.Embed(
            title=f"{emoji} 이적 완료!",
            description=f"**{선수명}** → **{team_name}** 이적 성공!",
            colour=0x2ECC71,
        )
        embed.add_field(name="이적료", value=f"{transfer_fee:,}원 (물가 반영)", inline=True)
        embed.add_field(name="선수단", value=f"{roster + 1}/{cap}명", inline=True)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  기본 선수 자동 세팅 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="기본선수세팅", description="유명 축구/야구 선수를 무소속으로 자동 등록합니다 (관리자)")
    @app_commands.checks.has_permissions(administrator=True)
    async def seed_default_players(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            from .player_data import SOCCER_PLAYERS, BASEBALL_PLAYERS
        except ImportError:
            return await interaction.followup.send("❌ `player_data.py` 파일을 찾을 수 없습니다.")

        added_soccer = 0
        added_baseball = 0

        async with aiosqlite.connect(self.db) as db:
            # 축구 선수 추가
            for p in SOCCER_PLAYERS:
                name, fee, p1, p2, p3, p4, p5, p6 = p
                cur = await db.execute("SELECT player_name FROM soccer_players WHERE player_name = ?", (name,))
                if not await cur.fetchone():
                    await db.execute(
                        """INSERT INTO soccer_players
                           (player_name, team_name, base_transfer_fee, pace, shooting, passing, dribbling, defending, physical)
                           VALUES (?, '무소속', ?, ?, ?, ?, ?, ?, ?)""",
                        (name, fee, p1, p2, p3, p4, p5, p6)
                    )
                    added_soccer += 1
            
            # 야구 선수 추가
            for p in BASEBALL_PLAYERS:
                name, fee, p1, p2, p3, p4, p5 = p
                cur = await db.execute("SELECT player_name FROM baseball_players WHERE player_name = ?", (name,))
                if not await cur.fetchone():
                    await db.execute(
                        """INSERT INTO baseball_players
                           (player_name, team_name, base_transfer_fee, contact, power, run, arm, field)
                           VALUES (?, '무소속', ?, ?, ?, ?, ?, ?)""",
                        (name, fee, p1, p2, p3, p4, p5)
                    )
                    added_baseball += 1

            await db.commit()

        embed = discord.Embed(
            title="✅ 기본 선수 세팅 완료",
            description="DB에 존재하지 않던 선수들만 새로 등록되었습니다.",
            colour=0x3498DB
        )
        embed.add_field(name="⚽ 축구 추가됨", value=f"{added_soccer}명", inline=True)
        embed.add_field(name="⚾ 야구 추가됨", value=f"{added_baseball}명", inline=True)
        await interaction.followup.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  선수 데이터 전체 초기화 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="선수초기화", description="등록된 모든 선수 데이터를 삭제합니다 (관리자)")
    @app_commands.checks.has_permissions(administrator=True)
    async def reset_all_players(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT COUNT(*) FROM soccer_players")
            soccer_count = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM baseball_players")
            baseball_count = (await cur.fetchone())[0]

            await db.execute("DELETE FROM soccer_players")
            await db.execute("DELETE FROM baseball_players")
            await db.commit()

        embed = discord.Embed(
            title="🗑️ 선수 데이터 초기화 완료",
            description="모든 선수가 DB에서 삭제되었습니다.\n`/기본선수세팅`으로 다시 등록하세요.",
            colour=0xE74C3C
        )
        embed.add_field(name="⚽ 삭제된 축구 선수", value=f"{soccer_count}명", inline=True)
        embed.add_field(name="⚾ 삭제된 야구 선수", value=f"{baseball_count}명", inline=True)
        await interaction.followup.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  시즌 운영 — 일정 생성 / 종목별 러너
    # ═════════════════════════════════════════════════════════
    async def start_season(self):
        """지금부터 자정 직전까지 종목별 일정을 새로 짜고 중계 러너를 띄운다."""
        await self.stop_runners()
        async with aiosqlite.connect(self.db) as db:
            teams = {}
            for sport in ("축구", "야구"):
                cur = await db.execute("SELECT team_name FROM sports_teams WHERE sport_type = ?", (sport,))
                teams[sport] = [r[0] for r in await cur.fetchall()]

        now = time.time()
        start = now + 30
        end = next_midnight_kst(now) - match_seconds() - 150
        for sport in ("축구", "야구"):
            self.schedule[sport] = build_schedule(sport, teams[sport], start, end)
            if self.schedule[sport]:
                self.runners[sport] = asyncio.create_task(self._run_sport(sport))
            print(f"[SPORTS] {sport} {len(teams[sport])}팀 → {self._schedule_summary(self.schedule[sport])}")

    async def stop_runners(self):
        tasks_ = [t for t in self.runners.values() if not t.done()]
        for t in tasks_:
            t.cancel()
        if tasks_:
            await asyncio.gather(*tasks_, return_exceptions=True)
        self.runners.clear()
        self.live = {"축구": None, "야구": None}

    async def _run_sport(self, sport):
        for fx in list(self.schedule[sport]):
            try:
                wait = fx.kickoff - time.time()
                if wait < -match_seconds():
                    continue  # 너무 늦은 경기(재시작 등)는 건너뜀
                if wait > PREVIEW_LEAD:
                    await asyncio.sleep(wait - PREVIEW_LEAD)
                if not await self._teams_exist(fx):
                    continue  # 구단 해체된 경기
                hp, ap = await self._load_players(sport, fx.home, fx.away)
                ch = await self.get_channel_for(sport)
                if ch:
                    await self._send_preview(ch, fx, hp, ap)
                wait = fx.kickoff - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.live[sport] = fx
                await self._play(fx, ch, hp, ap)
            except asyncio.CancelledError:
                raise
            except Exception:
                print(f"[SPORTS] {sport} 경기 처리 중 오류: {fx}")
                traceback.print_exc()
            finally:
                self.live[sport] = None

    async def _teams_exist(self, fx):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM sports_teams WHERE team_name IN (?, ?)", (fx.home, fx.away))
            return (await cur.fetchone())[0] == 2

    async def _load_players(self, sport, home, away):
        table = "soccer_players" if sport == "축구" else "baseball_players"
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            out = []
            for team in (home, away):
                cur = await db.execute(f"SELECT * FROM {table} WHERE team_name = ?", (team,))
                out.append([dict(r) for r in await cur.fetchall()])
        return out

    async def get_channel_for(self, sport):
        soc, bb = await self.get_channels()
        return soc if sport == "축구" else bb

    async def _standings(self, sport):
        """{팀명: (순위, row)}"""
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sports_teams WHERE sport_type = ?", (sport,))
            teams = [dict(r) for r in await cur.fetchall()]
        if sport == "축구":
            key = lambda t: (t["points"], t["wins"], -t["losses"])
        else:
            key = lambda t: (t["wins"] / max(t["wins"] + t["losses"], 1), t["wins"])
        teams.sort(key=key, reverse=True)
        return {t["team_name"]: (i + 1, t) for i, t in enumerate(teams)}

    def _morale(self, team):
        """최근 5경기 기세 -1.0 ~ +1.0 (설정 배율 반영)"""
        f = self.form.get(team, [])[-FORM_GAMES:]
        raw = (f.count("W") - f.count("L")) / FORM_GAMES
        return max(-1.0, min(1.0, raw * cfg.get("경기_폼영향")))

    def _form_str(self, team):
        f = self.form.get(team, [])[-FORM_GAMES:]
        return "".join({"W": "🟢", "D": "⚪", "L": "🔴"}[x] for x in f) or "기록 없음"

    def _streak(self, team):
        f = self.form.get(team, [])
        if not f:
            return None, 0
        last, n = f[-1], 0
        for x in reversed(f):
            if x != last:
                break
            n += 1
        return last, n

    def _sim_kwargs(self, fx):
        if fx.sport == "축구":
            return {"tune": cfg.tune("축구"), "form": (self._morale(fx.home), self._morale(fx.away))}
        return {"tune": cfg.tune("야구"),
                "form": (self._morale(fx.away), self._morale(fx.home)),
                "starters": (self.rotation.get(fx.away, 0), self.rotation.get(fx.home, 0))}

    @staticmethod
    def _record_str(sport, row):
        if not row:
            return "-"
        if sport == "축구":
            return f"{row['wins']}승 {row['draws']}무 {row['losses']}패 (승점 {row['points']})"
        tot = row["wins"] + row["losses"]
        return f"{row['wins']}승 {row['draws']}무 {row['losses']}패 (승률 {row['wins'] / max(tot, 1):.3f})"

    # ═════════════════════════════════════════════════════════
    #  프리뷰
    # ═════════════════════════════════════════════════════════
    async def _send_preview(self, ch, fx, hp, ap):
        meta = SPORT_META[fx.sport]
        st = await self._standings(fx.sport)
        kw = self._sim_kwargs(fx)
        extra = None
        if fx.sport == "축구":
            p1, pd, p2 = await asyncio.to_thread(win_odds, "축구", fx.home, fx.away, hp, ap, **kw)
            odds = _odds_bar(p1, pd, p2, fx.home, fx.away, "축구")
            matchup = f"🏠 **{fx.home}** vs **{fx.away}** ✈️"
        else:
            p_away, pd, p_home = await asyncio.to_thread(win_odds, "야구", fx.away, fx.home, ap, hp, **kw)
            odds = _odds_bar(p_away, pd, p_home, fx.away, fx.home, "야구")
            matchup = f"✈️ **{fx.away}** @ **{fx.home}** 🏠"
            sp_a = baseball_team_profile(ap, fx.away, kw["starters"][0])["starter"]
            sp_h = baseball_team_profile(hp, fx.home, kw["starters"][1])["starter"]
            extra = f"**{sp_a['player_name']}** (구위 {sp_a['arm']}) vs **{sp_h['player_name']}** (구위 {sp_h['arm']})"
        t = int(fx.kickoff)
        embed = discord.Embed(
            title=f"🔜 {meta['emoji']} {meta['league']} {fx.round}라운드 — 곧 시작합니다!",
            description=f"{matchup}\n⏰ 킥오프 <t:{t}:R>",
            colour=0x95A5A6,
        )
        for team in (fx.home, fx.away):
            rank, row = st.get(team, (None, None))
            embed.add_field(name=f"{team} {f'({rank}위)' if rank else ''}",
                            value=f"{self._record_str(fx.sport, row)}\n최근 {self._form_str(team)}", inline=True)
        if extra:
            embed.add_field(name="⚾ 선발 투수", value=extra, inline=False)
        embed.add_field(name="🔮 승부 예측", value=odds, inline=False)
        await ch.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  경기 진행 (시뮬레이션 → 실시간 중계 → 기록 반영)
    # ═════════════════════════════════════════════════════════
    async def _play(self, fx, ch, hp, ap):
        before = await self._standings(fx.sport)
        kw = self._sim_kwargs(fx)
        if fx.sport == "축구":
            match = await asyncio.to_thread(simulate_soccer, fx.home, fx.away, hp, ap, None, **kw)
            frames, clock = self._soccer_frames(match)
            render = lambda shown, el, pending=None, final=False: self._render_soccer(
                fx, match, shown, clock(el), pending, final)
            first, second = fx.home, fx.away
        else:
            match = await asyncio.to_thread(simulate_baseball, fx.away, fx.home, ap, hp, None, **kw)
            frames = self._baseball_frames(match)
            render = lambda shown, el, pending=None, final=False: self._render_baseball(
                fx, match, shown, pending, final)
            first, second = fx.away, fx.home

        if ch:
            await self._broadcast(ch, frames, render)

        s1, s2 = match.score
        await self.update_record(first, second, s1, s2, fx.sport)
        for team, mine, theirs in ((first, s1, s2), (second, s2, s1)):
            self.form.setdefault(team, []).append("W" if mine > theirs else "L" if mine < theirs else "D")
            self.form[team] = self.form[team][-10:]
            if fx.sport == "야구":
                self.rotation[team] = self.rotation.get(team, 0) + 1
        after = await self._standings(fx.sport)
        if ch:
            await self._send_final(ch, fx, match, first, second, before, after)

    async def _broadcast(self, ch, frames, render):
        loop = asyncio.get_running_loop()
        msg = await ch.send(embed=render(0, 0.0))
        start = loop.time()
        shift = 0.0          # 뜸 들인 시간만큼 타임라인을 뒤로 민다
        shown = 0
        last_edit = loop.time()
        try:
            while shown < len(frames):
                now = loop.time() - start - shift
                t_next, ev = frames[shown]
                if t_next > now:
                    if loop.time() - last_edit >= EDIT_INTERVAL:
                        msg = await self._safe_edit(ch, msg, render(shown, now))
                        last_edit = loop.time()
                    await asyncio.sleep(max(0.3, min(t_next - now, EDIT_INTERVAL)))
                    continue

                if ev.buildup:
                    msg = await self._safe_edit(ch, msg, render(shown, t_next, pending=ev.buildup))
                    await asyncio.sleep(suspense())
                    shift += suspense()

                alerts = []
                while True:
                    alerts.append(frames[shown][1].alert)
                    shown += 1
                    if (shown >= len(frames) or frames[shown][0] > now
                            or frames[shown][1].buildup):
                        break
                final = shown >= len(frames)
                msg = await self._safe_edit(ch, msg, render(shown, frames[shown - 1][0], final=final))
                last_edit = loop.time()
                for a in alerts:
                    if a:
                        await ch.send(a)
                if not final:
                    await asyncio.sleep(MIN_EDIT_GAP)
        except asyncio.CancelledError:
            try:
                e = render(shown, 0.0)
                e.set_footer(text="⛔ 시즌 일정이 재편성되어 중계가 중단되었습니다 (기록 미반영)")
                await msg.edit(embed=e)
            except Exception:
                pass
            raise

    async def _safe_edit(self, ch, msg, embed):
        try:
            await msg.edit(embed=embed)
            return msg
        except discord.NotFound:
            return await ch.send(embed=embed)  # 누가 전광판을 지웠으면 새로 띄움
        except discord.HTTPException:
            return msg

    # ── 축구 타임라인 ─────────────────────────────────────
    @staticmethod
    def _soccer_frames(match):
        ev = match.events
        n_bu = sum(1 for e in ev if e.buildup)
        total = max(match_seconds() * 0.6, match_seconds() - 8 - n_bu * suspense())
        first, ht = total * 0.47, total * 0.06
        second = total - first - ht
        len1, len2 = 45 + match.stoppage[0], 45 + match.stoppage[1]

        def at(e):
            if e.half == 1:
                return first * min(e.minute, len1) / len1
            if e.kind == "ko":
                return first + ht
            return first + ht + second * min(e.minute - 45, len2) / len2

        def label(abs_min, half):
            reg = 45 if half == 1 else 90
            return f"{abs_min}'" if abs_min <= reg else f"{reg}+{abs_min - reg}'"

        def clock(el):
            if el < first:
                return label(min(int(el / first * len1) + 1, len1), 1)
            if el < first + ht:
                return "하프타임"
            m = min(int((el - first - ht) / second * len2) + 1, len2)
            return label(45 + m, 2)

        return [(at(e), e) for e in ev], clock

    def _render_soccer(self, fx, match, shown, clock, pending, final):
        evs = match.events[:shown]
        last = evs[-1] if evs else None
        s = last.score if last else (0, 0)
        shots = last.shots if last else (0, 0)
        on_t = last.on_target if last else (0, 0)
        poss = last.poss if last else 50.0
        if final:
            clock = "경기 종료"
        elif last and last.kind == "ht":
            clock = "하프타임"

        embed = discord.Embed(
            title=(f"🏁 경기 종료 · ⚽ K리그 {fx.round}R" if final else f"🔴 LIVE · ⚽ K리그 {fx.round}R"),
            description=f"## {fx.home} {s[0]} : {s[1]} {fx.away}\n⏱️ **{clock}**",
            colour=0xF1C40F if final else 0xE74C3C,
        )
        scorers = [[], []]
        cards = [[0, 0], [0, 0]]
        for e in evs:
            if e.kind == "goal":
                scorers[e.team].append(f"{e.scorer} {e.label}")
            elif e.kind == "yellow":
                cards[e.team][0] += 1
            elif e.kind == "red":
                cards[e.team][1] += 1
        if scorers[0] or scorers[1]:
            embed.add_field(name="⚽ 득점", value=(
                f"**{fx.home}**: {', '.join(scorers[0]) or '-'}\n**{fx.away}**: {', '.join(scorers[1]) or '-'}"
            )[:1024], inline=False)
        embed.add_field(name="📊 기록", value=(
            f"슈팅 **{shots[0]}** - **{shots[1]}** · 유효 **{on_t[0]}** - **{on_t[1]}**\n"
            f"점유율 **{poss:.0f}%** - **{100 - poss:.0f}%** · "
            f"🟨{cards[0][0]} 🟥{cards[0][1]} - 🟨{cards[1][0]} 🟥{cards[1][1]}"
        ), inline=False)
        log = [e.text for e in evs][-LOG_LINES:]
        if pending:
            log.append(f"**{pending}**")
        embed.add_field(name="📺 문자중계", value=_trim_log(log), inline=False)
        embed.set_footer(text=f"🏠 {fx.home} · 5분 실시간 중계")
        return embed

    # ── 야구 타임라인 ─────────────────────────────────────
    @staticmethod
    def _baseball_frames(match):
        weights = {"pa": 1.0, "info": 0.6, "change": 0.6, "inning": 0.5, "end": 0.0}
        ev = match.events
        n_bu = sum(1 for e in ev if e.buildup)
        total = max(match_seconds() * 0.6, match_seconds() - 8 - n_bu * suspense())
        unit = total / max(sum(weights[e.kind] for e in ev), 1)
        frames, t = [], 0.0
        for e in ev:
            frames.append((t, e))
            t += weights[e.kind] * unit
        return frames

    def _render_baseball(self, fx, match, shown, pending, final):
        evs = match.events[:shown]
        last = evs[-1] if evs else None
        s = last.score if last else (0, 0)
        names = (fx.away, fx.home)

        # 라인스코어
        n_inn = max(9, max((e.inning for e in evs), default=1))
        line = [[None] * n_inn, [None] * n_inn]
        hits = [0, 0]
        for e in evs:
            k = 0 if e.top else 1
            if e.kind == "inning":
                line[k][e.inning - 1] = 0
            line[k][e.inning - 1] = (line[k][e.inning - 1] or 0) + e.runs
            hits[k] += 1 if e.hit else 0
        if final and s[1] > s[0] and line[1][match.innings - 1] is None:
            line[1][match.innings - 1] = "X"
        head = _pad("", 8) + "".join(f"{i + 1:>3}" for i in range(n_inn)) + " │  R  H"
        rows = [head]
        for k in (0, 1):
            cells = "".join(f"{'' if v is None else v:>3}" for v in line[k])
            rows.append(_pad(names[k], 8) + cells + f" │ {s[k]:>2} {hits[k]:>2}")
        board = "```\n" + "\n".join(rows) + "\n```"

        if final:
            status = "경기 종료"
        elif last is None:
            status = "플레이볼 대기"
        else:
            half = "초" if last.top else "말"
            outs = "●" * min(last.outs, 3) + "○" * (3 - min(last.outs, 3))
            status = f"{last.inning}회{half} · {outs}" + (" · 공수교대" if last.outs >= 3 else "")

        embed = discord.Embed(
            title=(f"🏁 경기 종료 · ⚾ KBO {fx.round}R" if final else f"🔴 LIVE · ⚾ KBO {fx.round}R"),
            description=f"## {fx.away} {s[0]} : {s[1]} {fx.home}\n⏱️ **{status}**\n{board}",
            colour=0xF1C40F if final else 0xE74C3C,
        )
        if last and not final and last.outs < 3:
            b = last.bases
            mark = lambda x: "◆" if x else "◇"
            diamond = f"```\n   {mark(b[1])}\n{mark(b[2])}     {mark(b[0])}\n   ⌂\n```"
            runners = ", ".join(f"{i + 1}루 {n}" for i, n in enumerate(b) if n) or "주자 없음"
            embed.add_field(name="💎 주자", value=f"{diamond}{runners}", inline=True)
        mound = next((e for e in reversed(evs) if e.pitcher), None)
        if mound and not final:
            pc = f" · {mound.pitches}구" if mound.kind == "pa" else " · 등판"
            embed.add_field(name="🎯 마운드", value=f"**{mound.pitcher}**{pc}", inline=True)
        log = [e.text for e in evs if e.kind != "inning"][-LOG_LINES:]
        if pending:
            log.append(f"**{pending}**")
        embed.add_field(name="📺 문자중계", value=_trim_log(log), inline=False)
        embed.set_footer(text=f"✈️ {fx.away} @ 🏠 {fx.home} · 5분 실시간 중계")
        return embed

    # ── 경기 후 결과 + 순위 변동 ──────────────────────────
    async def _send_final(self, ch, fx, match, first, second, before, after):
        meta = SPORT_META[fx.sport]
        s1, s2 = match.score
        if s1 > s2:
            result = f"🏆 **{first}** 승리!"
        elif s2 > s1:
            result = f"🏆 **{second}** 승리!"
        else:
            result = "🤝 무승부"

        lines = []
        for team in (first, second):
            b = before.get(team, (None, None))[0]
            a, row = after.get(team, (None, None))
            arrow = ""
            if b and a:
                arrow = " ▲" if a < b else (" ▼" if a > b else " -")
            lines.append(f"**{team}** {b or '?'}위 → **{a or '?'}위**{arrow} · {self._record_str(fx.sport, row)}")

        # MVP
        mvp = None
        if fx.sport == "축구":
            goals = {}
            for e in match.events:
                if e.kind == "goal":
                    goals[e.scorer] = goals.get(e.scorer, 0) + 1
            if goals:
                name, g = max(goals.items(), key=lambda kv: kv[1])
                mvp = f"**{name}** — {'해트트릭!! 🎩' if g >= 3 else f'{g}골'}"
        else:
            rbi = {}
            for e in match.events:
                if e.kind == "pa" and e.batter:
                    rbi[e.batter] = rbi.get(e.batter, 0) + e.runs * 2 + (1 if e.hit else 0) + (2 if "홈런" in e.text else 0)
            if rbi:
                name = max(rbi, key=rbi.get)
                hr = sum(1 for e in match.events if e.batter == name and "홈런" in e.text and e.kind == "pa")
                h = sum(1 for e in match.events if e.batter == name and e.hit)
                r = sum(e.runs for e in match.events if e.batter == name and e.kind == "pa")
                mvp = f"**{name}** — {h}안타 {r}타점" + (f" {hr}홈런" if hr else "")

        embed = discord.Embed(
            title=f"{meta['emoji']} FINAL · {first} {s1} : {s2} {second}",
            description=result,
            colour=0x2ECC71 if s1 != s2 else 0x95A5A6,
        )
        if mvp:
            embed.add_field(name="⭐ 오늘의 선수", value=mvp, inline=False)
        streaks = []
        for team in (first, second):
            kind, n = self._streak(team)
            if kind == "W" and n >= 3:
                streaks.append(f"🔥 **{team}** {n}연승 질주!")
            elif kind == "L" and n >= 3:
                streaks.append(f"💀 **{team}** {n}연패 수렁…")
        if streaks:
            embed.add_field(name="📈 기세", value="\n".join(streaks), inline=False)
        embed.add_field(name="📊 순위 변동", value="\n".join(lines), inline=False)
        nxt = next((f for f in self.schedule[fx.sport] if f.kickoff > time.time()), None)
        if nxt:
            embed.add_field(name="⏭️ 다음 경기",
                            value=f"**{nxt.home}** vs **{nxt.away}** · <t:{int(nxt.kickoff)}:R>", inline=False)
        await ch.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  DB 전적 업데이트
    # ═════════════════════════════════════════════════════════
    async def update_record(self, team_a: str, team_b: str, score_a: int, score_b: int, sport: str):
        """승/무/패 DB 업데이트 — 축구: 승 3점 / 무 1점, 야구: 승률(무승부 제외)"""
        win_pts = 3 if sport == "축구" else 1
        draw_pts = 1 if sport == "축구" else 0
        async with aiosqlite.connect(self.db) as db:
            if score_a == score_b:
                await db.execute(
                    "UPDATE sports_teams SET draws = draws + 1, points = points + ? WHERE team_name IN (?, ?)",
                    (draw_pts, team_a, team_b),
                )
            else:
                win, lose = (team_a, team_b) if score_a > score_b else (team_b, team_a)
                await db.execute(
                    "UPDATE sports_teams SET wins = wins + 1, points = points + ? WHERE team_name = ?",
                    (win_pts, win),
                )
                await db.execute(
                    "UPDATE sports_teams SET losses = losses + 1 WHERE team_name = ?", (lose,)
                )
            await db.commit()

    # ═════════════════════════════════════════════════════════
    #  중계 채널 가져오기
    # ═════════════════════════════════════════════════════════
    async def get_channels(self):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT soccer_channel_id, baseball_channel_id FROM server_settings WHERE id = 1"
            )
            row = await cur.fetchone()
        if not row:
            return None, None
        soc_ch = self.bot.get_channel(row[0]) if row[0] else None
        bb_ch = self.bot.get_channel(row[1]) if row[1] else None
        return soc_ch, bb_ch

    # ═════════════════════════════════════════════════════════
    #  turn_passed 이벤트 수신 → 시즌 초기화
    # ═════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_turn_passed(self):
        """경제 턴 넘기기 이벤트 수신 → 최종 랭킹 출력 + 시즌 리셋 + 새 일정"""
        await self.stop_runners()
        soccer_ch, baseball_ch = await self.get_channels()

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row

            # ── 축구 최종 랭킹 ────────────────────────────
            cur = await db.execute(
                """SELECT team_name, wins, draws, losses, points
                   FROM sports_teams WHERE sport_type = '축구'
                   ORDER BY points DESC, wins DESC"""
            )
            soccer_rank = await cur.fetchall()

            # ── 야구 최종 랭킹 ────────────────────────────
            cur = await db.execute(
                """SELECT team_name, wins, draws, losses, points
                   FROM sports_teams WHERE sport_type = '야구'
                   ORDER BY wins DESC, losses ASC"""
            )
            baseball_rank = await cur.fetchall()

            # ── 전적 초기화 ──────────────────────────────
            await db.execute(
                "UPDATE sports_teams SET wins = 0, draws = 0, losses = 0, points = 0"
            )
            await db.commit()

        # ── 축구 랭킹 Embed ────────────────────────────
        if soccer_rank and soccer_ch:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, r in enumerate(soccer_rank):
                medal = medals[i] if i < 3 else f"**{i+1}.**"
                total = r["wins"] + r["draws"] + r["losses"]
                lines.append(
                    f"{medal} **{r['team_name']}** — "
                    f"{r['wins']}승 {r['draws']}무 {r['losses']}패 "
                    f"({r['points']}점) [{total}경기]"
                )
            embed = discord.Embed(
                title="⚽ K리그 시즌 최종 순위",
                description="\n".join(lines),
                colour=0xF1C40F,
            )
            embed.set_footer(text="새 시즌이 시작됩니다!")
            await soccer_ch.send(embed=embed)

        # ── 야구 랭킹 Embed ────────────────────────────
        if baseball_rank and baseball_ch:
            lines = []
            medals = ["🥇", "🥈", "🥉"]
            for i, r in enumerate(baseball_rank):
                medal = medals[i] if i < 3 else f"**{i+1}.**"
                total = r["wins"] + r["losses"]
                win_rate = r["wins"] / max(total, 1)
                lines.append(
                    f"{medal} **{r['team_name']}** — "
                    f"{r['wins']}승 {r['losses']}패 "
                    f"(승률 {win_rate:.3f}) [{total}경기]"
                )
            embed = discord.Embed(
                title="⚾ KBO 시즌 최종 순위",
                description="\n".join(lines),
                colour=0xF1C40F,
            )
            embed.set_footer(text="새 시즌이 시작됩니다!")
            await baseball_ch.send(embed=embed)


        # ── 새 시즌 일정 ────────────────────────────────
        self.form.clear()
        await self.start_season()
        for sport, ch in (("축구", soccer_ch), ("야구", baseball_ch)):
            if ch and self.schedule[sport]:
                meta = SPORT_META[sport]
                embed = discord.Embed(
                    title=f"📅 {meta['league']} 새 시즌 개막!",
                    description=self._schedule_summary(self.schedule[sport]),
                    colour=0x2ECC71,
                )
                first = self.schedule[sport][0]
                embed.add_field(name="개막전", value=f"**{first.home}** vs **{first.away}** · <t:{int(first.kickoff)}:R>")
                await ch.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  관리자 — 리그/선수 수치 조정
    # ═════════════════════════════════════════════════════════
    STAT_COLUMNS = {
        "축구": {"스피드": "pace", "슈팅": "shooting", "패스": "passing", "드리블": "dribbling",
                 "수비": "defending", "피지컬": "physical"},
        "야구": {"컨택트": "contact", "파워": "power", "주루": "run", "송구": "arm", "수비": "field"},
    }

    @app_commands.command(name="전적수정", description="구단의 승/무/패/승점을 직접 수정합니다 (관리자)")
    @app_commands.describe(구단명="대상 구단", 승="승 수", 무="무 수", 패="패 수", 승점="승점 (비우면 축구는 승×3+무, 야구는 승)")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_set_record(self, interaction: discord.Interaction, 구단명: str, 승: int, 무: int, 패: int,
                               승점: int = None):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT sport_type FROM sports_teams WHERE team_name = ?", (구단명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 해당 구단이 존재하지 않습니다.", ephemeral=True)
            if 승점 is None:
                승점 = 승 * 3 + 무 if row[0] == "축구" else 승
            await db.execute(
                "UPDATE sports_teams SET wins = ?, draws = ?, losses = ?, points = ? WHERE team_name = ?",
                (승, 무, 패, 승점, 구단명),
            )
            await db.commit()
        await interaction.response.send_message(f"✏️ **{구단명}** 전적 → {승}승 {무}무 {패}패 (승점 {승점})")

    @app_commands.command(name="순위초기화", description="해당 종목 모든 구단의 이번 시즌 전적을 0으로 되돌립니다 (관리자)")
    @app_commands.describe(종목="축구 또는 야구")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_reset_standings(self, interaction: discord.Interaction, 종목: app_commands.Choice[str]):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "UPDATE sports_teams SET wins = 0, draws = 0, losses = 0, points = 0 WHERE sport_type = ?",
                (종목.value,),
            )
            n = cur.rowcount
            cur = await db.execute("SELECT team_name FROM sports_teams WHERE sport_type = ?", (종목.value,))
            teams = [r[0] for r in await cur.fetchall()]
            await db.commit()
        for team in teams:
            self.form.pop(team, None)
        await interaction.response.send_message(f"🧹 {종목.value} {n}개 구단 전적과 기세를 초기화했습니다.")

    async def _find_player(self, db, sport, name):
        table = "soccer_players" if sport == "축구" else "baseball_players"
        cur = await db.execute(f"SELECT * FROM {table} WHERE player_name = ?", (name,))
        return table, await cur.fetchone()

    @app_commands.command(name="선수능력치수정", description="선수 능력치를 수정합니다 (관리자)")
    @app_commands.describe(종목="축구 또는 야구", 선수명="대상 선수",
                           능력치="축구: 스피드/슈팅/패스/드리블/수비/피지컬 · 야구: 컨택트/파워/주루/송구/수비",
                           값="새 값 (1~99)")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_set_stat(self, interaction: discord.Interaction, 종목: app_commands.Choice[str],
                             선수명: str, 능력치: str, 값: int):
        cols = self.STAT_COLUMNS[종목.value]
        if 능력치 not in cols:
            return await interaction.response.send_message(
                f"❌ {종목.value} 능력치는 {', '.join(cols)} 중 하나입니다.", ephemeral=True)
        값 = max(1, min(99, 값))
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            table, row = await self._find_player(db, 종목.value, 선수명)
            if not row:
                return await interaction.response.send_message("❌ 해당 선수를 찾을 수 없습니다.", ephemeral=True)
            col = cols[능력치]
            await db.execute(f"UPDATE {table} SET {col} = ? WHERE player_name = ?", (값, 선수명))
            await db.commit()
        await interaction.response.send_message(f"📝 **{선수명}** {능력치} {row[col]} → **{값}**")

    @admin_set_stat.autocomplete("능력치")
    async def _stat_autocomplete(self, interaction: discord.Interaction, current: str):
        sport = getattr(interaction.namespace, "종목", None)
        names = list(self.STAT_COLUMNS.get(sport, {})) or sorted(
            set(self.STAT_COLUMNS["축구"]) | set(self.STAT_COLUMNS["야구"]))
        return [app_commands.Choice(name=n, value=n) for n in names if current in n][:25]

    @app_commands.command(name="선수몸값수정", description="선수의 기본 이적료(몸값)를 수정합니다 (관리자)")
    @app_commands.describe(종목="축구 또는 야구", 선수명="대상 선수", 금액="새 기본 이적료 (물가 반영 전)")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_set_fee(self, interaction: discord.Interaction, 종목: app_commands.Choice[str],
                            선수명: str, 금액: int):
        if 금액 < 0:
            return await interaction.response.send_message("❌ 금액은 0 이상이어야 합니다.", ephemeral=True)
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            table, row = await self._find_player(db, 종목.value, 선수명)
            if not row:
                return await interaction.response.send_message("❌ 해당 선수를 찾을 수 없습니다.", ephemeral=True)
            await db.execute(f"UPDATE {table} SET base_transfer_fee = ? WHERE player_name = ?", (금액, 선수명))
            await db.commit()
        await interaction.response.send_message(
            f"💰 **{선수명}** 몸값 {row['base_transfer_fee']:,} → **{금액:,}원**")

    @app_commands.command(name="선수강제이적", description="선수를 원하는 구단(또는 무소속)으로 옮깁니다 — 비용 없음 (관리자)")
    @app_commands.describe(종목="축구 또는 야구", 선수명="대상 선수", 구단명="이동할 구단 (무소속 가능)")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_move_player(self, interaction: discord.Interaction, 종목: app_commands.Choice[str],
                                선수명: str, 구단명: str):
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            table, row = await self._find_player(db, 종목.value, 선수명)
            if not row:
                return await interaction.response.send_message("❌ 해당 선수를 찾을 수 없습니다.", ephemeral=True)
            if 구단명 != "무소속":
                cur = await db.execute(
                    "SELECT 1 FROM sports_teams WHERE team_name = ? AND sport_type = ?", (구단명, 종목.value))
                if not await cur.fetchone():
                    return await interaction.response.send_message(
                        f"❌ {종목.value} 구단 **{구단명}**이(가) 없습니다.", ephemeral=True)
                cap = int(cfg.get(f"{종목.value}_최대선수"))
                cur = await db.execute(f"SELECT COUNT(*) FROM {table} WHERE team_name = ?", (구단명,))
                roster = (await cur.fetchone())[0]
                if roster >= cap and row["team_name"] != 구단명:
                    return await interaction.response.send_message(
                        f"❌ **{구단명}** 선수단이 가득 찼습니다 ({roster}/{cap}명). "
                        f"`/설정변경 {종목.value}_최대선수` 로 한도를 늘릴 수 있습니다.", ephemeral=True)
            await db.execute(f"UPDATE {table} SET team_name = ? WHERE player_name = ?", (구단명, 선수명))
            await db.commit()
        await interaction.response.send_message(f"🔀 **{선수명}**: {row['team_name']} → **{구단명}**")

    @app_commands.command(name="선수삭제", description="선수를 DB에서 삭제합니다 (관리자)")
    @app_commands.describe(종목="축구 또는 야구", 선수명="삭제할 선수")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_delete_player(self, interaction: discord.Interaction, 종목: app_commands.Choice[str], 선수명: str):
        table = "soccer_players" if 종목.value == "축구" else "baseball_players"
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(f"DELETE FROM {table} WHERE player_name = ?", (선수명,))
            n = cur.rowcount
            await db.commit()
        if not n:
            return await interaction.response.send_message("❌ 해당 선수를 찾을 수 없습니다.", ephemeral=True)
        await interaction.response.send_message(f"🗑️ {종목.value} 선수 **{선수명}** 삭제 완료")

    # ═════════════════════════════════════════════════════════
    #  선수 검색 및 비교
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="선수검색", description="선수의 능력치와 현재 몸값을 조회합니다")
    @app_commands.describe(종목="축구 또는 야구", 선수명="조회할 선수 이름")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구")
    ])
    async def search_player(self, interaction: discord.Interaction, 종목: app_commands.Choice[str], 선수명: str):
        await interaction.response.defer()
        
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            # 인플레이션 적용된 가격 계산용
            cur = await db.execute("SELECT cumulative_inflation FROM server_settings WHERE id = 1")
            inf_row = await cur.fetchone()
            inf = inf_row["cumulative_inflation"] if inf_row else 1.0

            if 종목.value == "축구":
                cur = await db.execute("SELECT * FROM soccer_players WHERE player_name = ?", (선수명,))
                row = await cur.fetchone()
                if not row:
                    return await interaction.followup.send(f"❌ '{선수명}' 선수를 찾을 수 없습니다.")
                
                value = int(row['base_transfer_fee'] * inf)
                embed = discord.Embed(title=f"⚽ {row['player_name']} (소속: {row['team_name']})", colour=0x3498DB)
                embed.add_field(name="현재 가치(몸값)", value=f"**{value:,}원**", inline=False)
                embed.add_field(name="스피드", value=row['pace'], inline=True)
                embed.add_field(name="슈팅", value=row['shooting'], inline=True)
                embed.add_field(name="패스", value=row['passing'], inline=True)
                embed.add_field(name="드리블", value=row['dribbling'], inline=True)
                embed.add_field(name="수비", value=row['defending'], inline=True)
                embed.add_field(name="피지컬", value=row['physical'], inline=True)
                await interaction.followup.send(embed=embed)

            else:
                cur = await db.execute("SELECT * FROM baseball_players WHERE player_name = ?", (선수명,))
                row = await cur.fetchone()
                if not row:
                    return await interaction.followup.send(f"❌ '{선수명}' 선수를 찾을 수 없습니다.")
                
                value = int(row['base_transfer_fee'] * inf)
                embed = discord.Embed(title=f"⚾ {row['player_name']} (소속: {row['team_name']})", colour=0xE74C3C)
                embed.add_field(name="현재 가치(몸값)", value=f"**{value:,}원**", inline=False)
                embed.add_field(name="컨택트", value=row['contact'], inline=True)
                embed.add_field(name="파워", value=row['power'], inline=True)
                embed.add_field(name="주루", value=row['run'], inline=True)
                embed.add_field(name="송구", value=row['arm'], inline=True)
                embed.add_field(name="수비", value=row['field'], inline=True)
                await interaction.followup.send(embed=embed)

    @app_commands.command(name="선수비교", description="두 선수의 스탯을 비교합니다")
    @app_commands.describe(종목="축구 또는 야구", 선수1="비교할 선수 1", 선수2="비교할 선수 2")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구")
    ])
    async def compare_players(self, interaction: discord.Interaction, 종목: app_commands.Choice[str], 선수1: str, 선수2: str):
        await interaction.response.defer()
        
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            if 종목.value == "축구":
                cur = await db.execute("SELECT * FROM soccer_players WHERE player_name IN (?, ?)", (선수1, 선수2))
                rows = await cur.fetchall()
            else:
                cur = await db.execute("SELECT * FROM baseball_players WHERE player_name IN (?, ?)", (선수1, 선수2))
                rows = await cur.fetchall()

        if len(rows) < 2:
            return await interaction.followup.send("❌ 한 명 이상의 선수를 찾을 수 없습니다. 이름을 정확히 입력하세요.")
        
        p1 = rows[0]
        p2 = rows[1]
        # p1이 선수1이 되도록 정렬
        if p1['player_name'] != 선수1:
            p1, p2 = p2, p1

        embed = discord.Embed(title=f"📊 {선수1} vs {선수2} 비교", colour=0x9B59B6)
        
        def compare(v1, v2):
            if v1 > v2: return f"**{v1}** > {v2}"
            elif v1 < v2: return f"{v1} < **{v2}**"
            else: return f"{v1} = {v2}"

        if 종목.value == "축구":
            embed.add_field(name="스피드", value=compare(p1['pace'], p2['pace']), inline=False)
            embed.add_field(name="슈팅", value=compare(p1['shooting'], p2['shooting']), inline=False)
            embed.add_field(name="패스", value=compare(p1['passing'], p2['passing']), inline=False)
            embed.add_field(name="드리블", value=compare(p1['dribbling'], p2['dribbling']), inline=False)
            embed.add_field(name="수비", value=compare(p1['defending'], p2['defending']), inline=False)
            embed.add_field(name="피지컬", value=compare(p1['physical'], p2['physical']), inline=False)
        else:
            embed.add_field(name="컨택트", value=compare(p1['contact'], p2['contact']), inline=False)
            embed.add_field(name="파워", value=compare(p1['power'], p2['power']), inline=False)
            embed.add_field(name="주루", value=compare(p1['run'], p2['run']), inline=False)
            embed.add_field(name="송구", value=compare(p1['arm'], p2['arm']), inline=False)
            embed.add_field(name="수비", value=compare(p1['field'], p2['field']), inline=False)

        await interaction.followup.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  선수 명단 조회  (페이지네이션)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="선수명단", description="등록된 선수 목록을 조회합니다 (페이지별 10명)")
    @app_commands.describe(종목="축구 또는 야구", 페이지="페이지 번호 (기본 1)", 팀명="특정 팀의 선수만 봅니다 (선택사항)")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구")
    ])
    async def player_list(self, interaction: discord.Interaction, 종목: app_commands.Choice[str], 페이지: int = 1, 팀명: str = None):
        await interaction.response.defer()
        PER_PAGE = 10

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            # 인플레이션
            cur = await db.execute("SELECT cumulative_inflation FROM server_settings WHERE id = 1")
            inf_row = await cur.fetchone()
            inf = inf_row["cumulative_inflation"] if inf_row else 1.0

            if 종목.value == "축구":
                query = "SELECT player_name, team_name, base_transfer_fee, pace, shooting, passing, dribbling, defending, physical FROM soccer_players"
                params = ()
                if 팀명:
                    query += " WHERE team_name = ?"
                    params = (팀명,)
                query += " ORDER BY CASE WHEN team_name = '무소속' THEN 1 ELSE 0 END, team_name ASC, base_transfer_fee DESC"
                cur = await db.execute(query, params)
            else:
                query = "SELECT player_name, team_name, base_transfer_fee, contact, power, run, arm, field FROM baseball_players"
                params = ()
                if 팀명:
                    query += " WHERE team_name = ?"
                    params = (팀명,)
                query += " ORDER BY CASE WHEN team_name = '무소속' THEN 1 ELSE 0 END, team_name ASC, base_transfer_fee DESC"
                cur = await db.execute(query, params)
                
            all_rows = await cur.fetchall()

        total = len(all_rows)
        max_page = max(1, math.ceil(total / PER_PAGE))
        page = max(1, min(페이지, max_page))
        start = (page - 1) * PER_PAGE
        end = start + PER_PAGE
        page_rows = all_rows[start:end]

        lines = []
        for i, r in enumerate(page_rows, start + 1):
            value = int(r["base_transfer_fee"] * inf)
            team = r["team_name"] if r["team_name"] != "무소속" else "FA(무소속)"
            if 종목.value == "축구":
                overall = int((r["pace"] + r["shooting"] + r["passing"] + r["dribbling"] + r["defending"] + r["physical"]) / 6)
                lines.append(f"`{i:>3}.` **{r['player_name']}** [{team}] — 💰{value:,}원 | OVR {overall}")
            else:
                overall = int((r["contact"] + r["power"] + r["run"] + r["arm"] + r["field"]) / 5)
                lines.append(f"`{i:>3}.` **{r['player_name']}** [{team}] — 💰{value:,}원 | OVR {overall}")

        emoji = "⚽" if 종목.value == "축구" else "⚾"
        title_text = f"{emoji} {종목.value} {팀명 if 팀명 else '전체'} 선수 명단 (총 {total}명)"
        embed = discord.Embed(
            title=title_text,
            description="\n".join(lines) if lines else "해당 조건의 선수가 없습니다.",
            colour=0x3498DB if 종목.value == "축구" else 0xE74C3C
        )
        embed.set_footer(text=f"📄 {page} / {max_page} 페이지")
        await interaction.followup.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  리그 팀 순위 조회
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="리그순위", description="현재 시즌 리그 순위를 언제든지 조회합니다")
    @app_commands.describe(종목="축구 또는 야구")
    @app_commands.choices(종목=[
        app_commands.Choice(name="축구", value="축구"),
        app_commands.Choice(name="야구", value="야구")
    ])
    async def league_ranking(self, interaction: discord.Interaction, 종목: app_commands.Choice[str]):
        await interaction.response.defer()
        
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sports_teams WHERE sport_type = ?", (종목.value,)
            )
            teams = await cur.fetchall()

        if not teams:
            return await interaction.followup.send(f"❌ {종목.value} 구단이 아직 창설되지 않았습니다.")

        lines = []
        if 종목.value == "축구":
            sorted_teams = sorted(teams, key=lambda t: (t["points"], t["wins"]), reverse=True)
            for i, t in enumerate(sorted_teams, 1):
                total = t["wins"] + t["draws"] + t["losses"]
                lines.append(f"**{i}위** {t['team_name']} — 승점 {t['points']}점 ({t['wins']}승 {t['draws']}무 {t['losses']}패) [{total}경기]")
            embed = discord.Embed(title="⚽ K리그 현재 순위", description="\n".join(lines), colour=0x3498DB)
        else:
            def win_rate(t):
                tot = t["wins"] + t["losses"]
                return t["wins"] / tot if tot > 0 else 0
            sorted_teams = sorted(teams, key=lambda t: (win_rate(t), t["wins"]), reverse=True)
            for i, t in enumerate(sorted_teams, 1):
                total = t["wins"] + t["losses"]
                lines.append(f"**{i}위** {t['team_name']} — 승률 {win_rate(t):.3f} ({t['wins']}승 {t['losses']}패) [{total}경기]")
            embed = discord.Embed(title="⚾ KBO 현재 순위", description="\n".join(lines), colour=0xF1C40F)

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SportsCog(bot))
