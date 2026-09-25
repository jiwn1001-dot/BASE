"""
cogs/sports.py — 스포츠 시뮬레이션 시스템
K리그(축구) · KBO(야구) 구단 관리 · 선수 이적 · 24시간 자동 중계
10분 루프 스케줄러 / 시즌 큐 / 스탯 기반 텍스트 중계
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import random
import math
from datetime import datetime

# ─── 팀 수 제한 ──────────────────────────────────────────────
MAX_SOCCER_TEAMS = 12
MAX_BASEBALL_TEAMS = 10

# ─── 축구 이벤트 텍스트 템플릿 ─────────────────────────────
SOCCER_GOAL_TEXTS = [
    "⚽ {minute}분 | **{player}**의 강력한 슛! 골망을 가릅니다! 🔥",
    "⚽ {minute}분 | **{player}**의 환상적인 중거리 슛이 골대 구석으로!",
    "⚽ {minute}분 | **{player}**, 수비를 제치고 침착한 마무리! 골!!",
    "⚽ {minute}분 | **{player}**의 헤딩 골! 크로스를 정확히 맞추었습니다!",
    "⚽ {minute}분 | **{player}**의 프리킥이 벽을 넘어 골인!",
    "⚽ {minute}분 | **{player}**, 패스를 받아 원터치 골! 아름다운 팀워크!",
]

SOCCER_SAVE_TEXTS = [
    "🧤 {minute}분 | **{player}**의 슛! {team_b} 골키퍼의 선방으로 막힙니다!",
    "🧤 {minute}분 | **{player}**의 강슛이 크로스바를 강타!",
    "🧤 {minute}분 | **{player}**의 슛, 아깝게 빗나갑니다!",
]

SOCCER_EVENT_TEXTS = [
    "💨 {minute}분 | **{player}**의 빠른 드리블 돌파! 수비가 허를 찔렸습니다!",
    "🌀 {minute}분 | **{player}**의 현란한 개인기로 상대를 농락합니다!",
    "🎯 {minute}분 | **{player}**의 정교한 스루패스! 찬스 생성!",
    "🛡️ {minute}분 | **{player}**의 완벽한 태클! 상대 공격을 차단!",
    "🟨 {minute}분 | **{player}**에게 경고 카드가 주어집니다.",
    "🚩 {minute}분 | {team} 코너킥 기회!",
]

# ─── 야구 이벤트 텍스트 템플릿 ─────────────────────────────
BASEBALL_HIT_TEXTS = {
    "안타": [
        "🏏 {inning}회 | **{batter}**의 깔끔한 안타! 1루로 출루!",
        "🏏 {inning}회 | **{batter}**, 타구가 야수 사이를 꿰뚫습니다! 안타!",
    ],
    "2루타": [
        "🏏 {inning}회 | **{batter}**의 강렬한 2루타! 외야 사이로 빠집니다!",
        "🏏 {inning}회 | **{batter}**, 펜스 앞에서 바운드! 2루타!",
    ],
    "3루타": [
        "🏏 {inning}회 | **{batter}**의 통쾌한 3루타! 외야 깊숙이!",
    ],
    "홈런": [
        "💥 {inning}회 | **{batter}**의 호쾌한 홈런! 담장을 넘겼습니다! 🎆",
        "💥 {inning}회 | **{batter}**, 풀스윙! 공이 하늘 높이 날아갑니다! 홈런!!",
        "💥 {inning}회 | **{batter}**의 역전 홈런! 관중석이 들썩입니다!",
    ],
    "만루홈런": [
        "🔥 {inning}회 | **{batter}**의 만루홈런!!! 4점이 한꺼번에!! 🎇🎆",
    ],
}

BASEBALL_OUT_TEXTS = [
    "🙅 {inning}회 | **{batter}** 헛스윙 삼진! 배트를 던집니다.",
    "🙅 {inning}회 | **{batter}** 루킹 삼진. 꼼짝없이 당했습니다.",
    "🦅 {inning}회 | **{batter}**의 뜬공, **{fielder}**가 여유있게 잡아냅니다!",
    "⬇️ {inning}회 | **{batter}** 땅볼 아웃! 내야수의 빠른 송구!",
    "⬇️ {inning}회 | **{batter}** 병살타! 한 번에 두 아웃!",
]

BASEBALL_STEAL_TEXTS = [
    "💨 {inning}회 | **{runner}**의 도루 성공! 빠른 발을 보여줍니다!",
    "🚫 {inning}회 | **{runner}** 도루 실패! 포수의 정확한 송구에 아웃!",
]


class SportsCog(commands.Cog):
    """스포츠(축구/야구) 시뮬레이션 관리 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 시즌 큐
        self.soccer_queue: list[tuple[str, str]] = []
        self.baseball_queue: list[tuple[str, str]] = []
        self.season_active = False
        # 10분 루프 시작
        self.match_scheduler.start()

    def cog_unload(self):
        self.match_scheduler.cancel()

    @property
    def db(self):
        return self.bot.db_path

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

            table = "soccer_players" if sport == "축구" else "baseball_players"
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
    #  시즌 생성
    # ═════════════════════════════════════════════════════════
    async def generate_season(self):
        """등록된 팀 수에 따라 시즌 스케줄 생성"""
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT team_name FROM sports_teams WHERE sport_type = '축구'"
            )
            soccer_teams = [r["team_name"] for r in await cur.fetchall()]

            cur = await db.execute(
                "SELECT team_name FROM sports_teams WHERE sport_type = '야구'"
            )
            baseball_teams = [r["team_name"] for r in await cur.fetchall()]

        # ── 축구 스케줄 (K리그 스타일) ────────────────────
        self.soccer_queue = []
        n_soc = len(soccer_teams)
        if n_soc >= 2:
            # 12팀 기준 38경기 → 팀 수에 따라 비례 조정
            games_per_pair = max(2, round(38 / max(n_soc - 1, 1)))
            for i in range(n_soc):
                for j in range(i + 1, n_soc):
                    for k in range(games_per_pair):
                        if k % 2 == 0:
                            self.soccer_queue.append(
                                (soccer_teams[i], soccer_teams[j])
                            )
                        else:
                            self.soccer_queue.append(
                                (soccer_teams[j], soccer_teams[i])
                            )
            random.shuffle(self.soccer_queue)

        # ── 야구 스케줄 (KBO 스타일) ─────────────────────
        self.baseball_queue = []
        n_bb = len(baseball_teams)
        if n_bb >= 2:
            # 10팀 기준 144경기 → 팀 수에 따라 비례 조정
            games_per_pair = max(2, round(144 / max(n_bb - 1, 1)))
            for i in range(n_bb):
                for j in range(i + 1, n_bb):
                    for k in range(games_per_pair):
                        if k % 2 == 0:
                            self.baseball_queue.append(
                                (baseball_teams[i], baseball_teams[j])
                            )
                        else:
                            self.baseball_queue.append(
                                (baseball_teams[j], baseball_teams[i])
                            )
            random.shuffle(self.baseball_queue)

        self.season_active = True
        total = len(self.soccer_queue) + len(self.baseball_queue)
        print(
            f"[SPORTS] 시즌 생성 완료 — "
            f"축구 {len(self.soccer_queue)}경기 ({n_soc}팀) / "
            f"야구 {len(self.baseball_queue)}경기 ({n_bb}팀) / "
            f"총 {total}경기"
        )

    # ═════════════════════════════════════════════════════════
    #  축구 경기 시뮬레이션 (FIFA 6스탯 기반)
    # ═════════════════════════════════════════════════════════
    async def simulate_soccer(self, team_a: str, team_b: str) -> tuple[int, int, list[str]]:
        """FIFA 스탯 기반 축구 매치 시뮬레이션"""
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM soccer_players WHERE team_name = ?", (team_a,)
            )
            players_a = await cur.fetchall()
            cur = await db.execute(
                "SELECT * FROM soccer_players WHERE team_name = ?", (team_b,)
            )
            players_b = await cur.fetchall()

        # 선수가 없으면 더미 선수
        if not players_a:
            players_a = [{"player_name": f"{team_a} 무명선수", "pace": 50, "shooting": 50,
                          "passing": 50, "dribbling": 50, "defending": 50, "physical": 50}]
        if not players_b:
            players_b = [{"player_name": f"{team_b} 무명선수", "pace": 50, "shooting": 50,
                          "passing": 50, "dribbling": 50, "defending": 50, "physical": 50}]

        # 팀 평균 스탯 계산
        def team_avg(players, *stats):
            total = sum(sum(p[s] for s in stats) for p in players)
            return total / (len(players) * len(stats))

        attack_a = team_avg(players_a, "pace", "shooting", "dribbling", "passing")
        defense_a = team_avg(players_a, "defending", "physical")
        attack_b = team_avg(players_b, "pace", "shooting", "dribbling", "passing")
        defense_b = team_avg(players_b, "defending", "physical")

        score_a, score_b = 0, 0
        events = []

        for minute in range(1, 91):
            # 약 5~8% 확률로 공격 이벤트 발생
            chance = 0.04 + (attack_a + attack_b) / 5000
            if random.random() > chance:
                continue

            # 어느 팀이 공격하는지
            attack_weight_a = attack_a / max(attack_a + attack_b, 1)
            is_a_attacking = random.random() < attack_weight_a

            if is_a_attacking:
                attacker = random.choice(players_a)
                att_power = (
                    attacker["shooting"] * 0.4
                    + attacker["pace"] * 0.2
                    + attacker["dribbling"] * 0.25
                    + attacker["passing"] * 0.15
                    + random.uniform(-15, 15)
                )
                def_power = defense_b + random.uniform(-10, 10)
            else:
                attacker = random.choice(players_b)
                att_power = (
                    attacker["shooting"] * 0.4
                    + attacker["pace"] * 0.2
                    + attacker["dribbling"] * 0.25
                    + attacker["passing"] * 0.15
                    + random.uniform(-15, 15)
                )
                def_power = defense_a + random.uniform(-10, 10)

            goal_chance = att_power / max(att_power + def_power, 1)

            if random.random() < goal_chance * 0.45:
                # 골!
                if is_a_attacking:
                    score_a += 1
                else:
                    score_b += 1
                template = random.choice(SOCCER_GOAL_TEXTS)
                events.append(template.format(
                    minute=minute, player=attacker["player_name"]
                ))
            elif random.random() < 0.5:
                # 세이브/빗나감
                defending_team = team_b if is_a_attacking else team_a
                template = random.choice(SOCCER_SAVE_TEXTS)
                events.append(template.format(
                    minute=minute, player=attacker["player_name"],
                    team_b=defending_team
                ))
            else:
                # 기타 이벤트
                event_player = random.choice(
                    players_a if is_a_attacking else players_b
                )
                attacking_team = team_a if is_a_attacking else team_b
                template = random.choice(SOCCER_EVENT_TEXTS)
                events.append(template.format(
                    minute=minute, player=event_player["player_name"],
                    team=attacking_team
                ))

        return score_a, score_b, events

    # ═════════════════════════════════════════════════════════
    #  야구 경기 시뮬레이션 (5툴 기반)
    # ═════════════════════════════════════════════════════════
    async def simulate_baseball(self, team_a: str, team_b: str) -> tuple[int, int, list[str]]:
        """5-tool 스탯 기반 야구 매치 시뮬레이션"""
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM baseball_players WHERE team_name = ?", (team_a,)
            )
            players_a = await cur.fetchall()
            cur = await db.execute(
                "SELECT * FROM baseball_players WHERE team_name = ?", (team_b,)
            )
            players_b = await cur.fetchall()

        # 선수 없으면 더미 생성
        if not players_a:
            players_a = [{"player_name": f"{team_a} 무명타자", "contact": 50,
                          "power": 50, "run": 50, "arm": 50, "field": 50}]
        if not players_b:
            players_b = [{"player_name": f"{team_b} 무명타자", "contact": 50,
                          "power": 50, "run": 50, "arm": 50, "field": 50}]

        score_a, score_b = 0, 0
        events = []

        def team_defense_avg(players):
            return sum(p["arm"] + p["field"] for p in players) / (len(players) * 2)

        def simulate_half_inning(batting_team, batting_players, fielding_players, inning, is_top):
            """하프이닝 시뮬레이션. 반환: (득점, 이벤트 리스트)"""
            nonlocal events
            outs = 0
            bases = [False, False, False]  # 1루, 2루, 3루
            runs = 0
            half_label = "초" if is_top else "말"
            defense_avg = team_defense_avg(fielding_players)
            batter_index = 0

            while outs < 3:
                batter = batting_players[batter_index % len(batting_players)]
                batter_index += 1

                # 타격 판정: contact vs 수비
                hit_chance = batter["contact"] / (batter["contact"] + defense_avg + 20)
                roll = random.random()

                if roll < hit_chance:
                    # 안타 종류 결정 (power 기반)
                    power_roll = random.random() * 100
                    power_factor = batter["power"]

                    if power_factor > 80 and power_roll < power_factor * 0.12:
                        # 홈런 판정
                        runners_on = sum(1 for b in bases if b)
                        rbi = 1 + runners_on
                        runs += rbi
                        bases = [False, False, False]
                        if runners_on == 3:
                            template = random.choice(BASEBALL_HIT_TEXTS["만루홈런"])
                        else:
                            template = random.choice(BASEBALL_HIT_TEXTS["홈런"])
                        events.append(template.format(
                            inning=f"{inning}회{half_label}", batter=batter["player_name"]
                        ))
                    elif power_roll < power_factor * 0.25:
                        # 2루타
                        if bases[2]:
                            runs += 1
                        if bases[1]:
                            runs += 1
                        bases[2] = bases[0]
                        bases[1] = True
                        bases[0] = False
                        template = random.choice(BASEBALL_HIT_TEXTS["2루타"])
                        events.append(template.format(
                            inning=f"{inning}회{half_label}", batter=batter["player_name"]
                        ))
                    elif power_roll < power_factor * 0.30:
                        # 3루타
                        scored = sum(1 for b in bases if b)
                        runs += scored
                        bases = [False, False, True]
                        template = random.choice(BASEBALL_HIT_TEXTS["3루타"])
                        events.append(template.format(
                            inning=f"{inning}회{half_label}", batter=batter["player_name"]
                        ))
                    else:
                        # 안타 (1루타)
                        if bases[2]:
                            runs += 1
                            bases[2] = False
                        # 주자 진루
                        if bases[1]:
                            bases[2] = True
                            bases[1] = False
                        if bases[0]:
                            bases[1] = True
                        bases[0] = True
                        template = random.choice(BASEBALL_HIT_TEXTS["안타"])
                        events.append(template.format(
                            inning=f"{inning}회{half_label}", batter=batter["player_name"]
                        ))

                    # 도루 시도 (run 스탯 기반)
                    if bases[0] and batter["run"] > 65 and random.random() < batter["run"] / 300:
                        if random.random() < batter["run"] / (batter["run"] + defense_avg):
                            # 도루 성공
                            if not bases[1]:
                                bases[1] = True
                                bases[0] = False
                                events.append(random.choice(BASEBALL_STEAL_TEXTS).format(
                                    inning=f"{inning}회{half_label}",
                                    runner=batter["player_name"]
                                ))
                        else:
                            # 도루 실패
                            outs += 1
                            bases[0] = False
                            events.append(BASEBALL_STEAL_TEXTS[1].format(
                                inning=f"{inning}회{half_label}",
                                runner=batter["player_name"]
                            ))
                else:
                    # 아웃
                    outs += 1
                    fielder = random.choice(fielding_players)
                    template = random.choice(BASEBALL_OUT_TEXTS)
                    events.append(template.format(
                        inning=f"{inning}회{half_label}",
                        batter=batter["player_name"],
                        fielder=fielder["player_name"],
                    ))

            return runs

        # 9이닝 진행
        for inning in range(1, 10):
            # 상위 (team_a 공격)
            score_a += simulate_half_inning(team_a, players_a, players_b, inning, True)
            # 하위 (team_b 공격)
            score_b += simulate_half_inning(team_b, players_b, players_a, inning, False)

        return score_a, score_b, events

    # ═════════════════════════════════════════════════════════
    #  DB 전적 업데이트
    # ═════════════════════════════════════════════════════════
    async def update_record(self, team_a: str, team_b: str, score_a: int, score_b: int, sport: str):
        """승/무/패 DB 업데이트"""
        async with aiosqlite.connect(self.db) as db:
            if score_a > score_b:
                win, lose = team_a, team_b
                if sport == "축구":
                    await db.execute(
                        "UPDATE sports_teams SET wins = wins + 1, points = points + 3 WHERE team_name = ?",
                        (win,),
                    )
                    await db.execute(
                        "UPDATE sports_teams SET losses = losses + 1 WHERE team_name = ?",
                        (lose,),
                    )
                else:
                    await db.execute(
                        "UPDATE sports_teams SET wins = wins + 1, points = points + 1 WHERE team_name = ?",
                        (win,),
                    )
                    await db.execute(
                        "UPDATE sports_teams SET losses = losses + 1 WHERE team_name = ?",
                        (lose,),
                    )
            elif score_a < score_b:
                win, lose = team_b, team_a
                if sport == "축구":
                    await db.execute(
                        "UPDATE sports_teams SET wins = wins + 1, points = points + 3 WHERE team_name = ?",
                        (win,),
                    )
                    await db.execute(
                        "UPDATE sports_teams SET losses = losses + 1 WHERE team_name = ?",
                        (lose,),
                    )
                else:
                    await db.execute(
                        "UPDATE sports_teams SET wins = wins + 1, points = points + 1 WHERE team_name = ?",
                        (win,),
                    )
                    await db.execute(
                        "UPDATE sports_teams SET losses = losses + 1 WHERE team_name = ?",
                        (lose,),
                    )
            else:
                # 무승부 (축구만 해당, 야구는 연장으로 무승부 없다고 가정)
                if sport == "축구":
                    await db.execute(
                        "UPDATE sports_teams SET draws = draws + 1, points = points + 1 WHERE team_name = ?",
                        (team_a,),
                    )
                    await db.execute(
                        "UPDATE sports_teams SET draws = draws + 1, points = points + 1 WHERE team_name = ?",
                        (team_b,),
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
    #  경기 결과 Embed 전송
    # ═════════════════════════════════════════════════════════
    async def send_match_result(self, channel, sport_emoji, team_a, team_b, score_a, score_b, events):
        """경기 결과를 중계 채널에 전송"""
        if not channel:
            return

        # 주요 이벤트만 선별 (최대 12개)
        key_events = [e for e in events if any(k in e for k in ["⚽", "💥", "🔥", "🏏"])]
        if not key_events:
            key_events = events[:6]
        else:
            key_events = key_events[:12]

        if score_a > score_b:
            result_text = f"🏆 **{team_a}** 승리!"
        elif score_b > score_a:
            result_text = f"🏆 **{team_b}** 승리!"
        else:
            result_text = "🤝 무승부!"

        embed = discord.Embed(
            title=f"{sport_emoji} {team_a} vs {team_b}",
            description=f"## {score_a} : {score_b}\n{result_text}",
            colour=0x2ECC71 if score_a > score_b else (0xE74C3C if score_b > score_a else 0x95A5A6),
        )

        if key_events:
            # 이벤트를 2000자 이내로 제한
            event_text = "\n".join(key_events)
            if len(event_text) > 1024:
                event_text = "\n".join(key_events[:8])
            embed.add_field(name="📺 주요 장면", value=event_text, inline=False)

        embed.set_footer(text=f"가상 국가 스포츠 리그 · {datetime.now().strftime('%H:%M')}")
        await channel.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  10분 루프 스케줄러
    # ═════════════════════════════════════════════════════════
    @tasks.loop(minutes=10)
    async def match_scheduler(self):
        """10분마다 시즌 큐에서 경기를 소화하고 중계한다."""
        if not self.season_active:
            return

        if not self.soccer_queue and not self.baseball_queue:
            self.season_active = False
            return

        # 현재 시각 기준 남은 10분 턴 수 (하루 144턴)
        now = datetime.now()
        minutes_since_midnight = now.hour * 60 + now.minute
        current_turn = minutes_since_midnight // 10
        remaining_turns = max(1, 144 - current_turn)

        soccer_ch, baseball_ch = await self.get_channels()

        # ── 축구 경기 소화 ──────────────────────────────
        if self.soccer_queue:
            games_this_turn = max(1, math.ceil(len(self.soccer_queue) / remaining_turns))
            for _ in range(min(games_this_turn, len(self.soccer_queue))):
                team_a, team_b = self.soccer_queue.pop(0)
                score_a, score_b, events = await self.simulate_soccer(team_a, team_b)
                await self.update_record(team_a, team_b, score_a, score_b, "축구")
                await self.send_match_result(soccer_ch, "⚽", team_a, team_b, score_a, score_b, events)

        # ── 야구 경기 소화 ──────────────────────────────
        if self.baseball_queue:
            games_this_turn = max(1, math.ceil(len(self.baseball_queue) / remaining_turns))
            for _ in range(min(games_this_turn, len(self.baseball_queue))):
                team_a, team_b = self.baseball_queue.pop(0)
                score_a, score_b, events = await self.simulate_baseball(team_a, team_b)
                await self.update_record(team_a, team_b, score_a, score_b, "야구")
                await self.send_match_result(baseball_ch, "⚾", team_a, team_b, score_a, score_b, events)

    @match_scheduler.before_loop
    async def before_scheduler(self):
        await self.bot.wait_until_ready()
        # 봇 시작 시 시즌이 없으면 생성
        if not self.season_active:
            await self.generate_season()

    # ═════════════════════════════════════════════════════════
    #  turn_passed 이벤트 수신 → 시즌 초기화
    # ═════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_turn_passed(self):
        """경제 턴 넘기기 이벤트 수신 → 최종 랭킹 출력 + 시즌 리셋"""
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

        # ── 새 시즌 생성 ─────────────────────────────────
        await self.generate_season()

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
