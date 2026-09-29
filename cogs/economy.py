"""
cogs/economy.py — 가상 국가 경제 시스템
복리 물가 · 개인 지갑 · 주식 시장 · 턴 넘기기
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
import random
import math
import time
from datetime import datetime, time as time_of_day, timezone, timedelta

from . import _config as cfg

KST = timezone(timedelta(hours=9))

# ─── 허용 섹터 ────────────────────────────────────────────────
VALID_SECTORS = [
    "에너지화학", "소재", "산업재", "모빌리티",
    "정보기술", "금융 및 부동산", "소비재", "헬스케어",
    "미디어 및 콘텐츠",
]

# ─── 주식 시장 ──────────────────────────────────────────────
# 주가 = 적정가(펀더멘털) 주변을 랜덤워크하며, 멀어지면 서서히 되돌아온다(평균회귀).
#   · 시장 전체 · 같은 섹터 · 개별 기업 충격이 섞여서 움직임 (같이 오르고 같이 빠짐)
#   · 개별 뉴스 / 시장 이벤트 / 상·하한가 / 추세(모멘텀)
#   · 매수·매도로 밀어 올린 가격은 몇 시간에 걸쳐 적정가로 복귀
# 모든 수치는 /설정변경 으로 실시간 조절 (cogs/_config.py 참고)

# 경기 단계: (이름, 이모지, 뉴스가 호재일 확률) — 수익률/변동성은 설정값 사용
PHASES = {
    1: ("불황", "⬇️", 0.35),
    2: ("회복기", "🌱", 0.60),
    3: ("보통", "📊", 0.50),
    4: ("경기과열", "📈", 0.60),
    5: ("공황", "💥", 0.20),
}

GOOD_NEWS = [
    "{c}, 대규모 신규 수주 계약 체결!",
    "{c}, 분기 실적 어닝 서프라이즈!",
    "{c}, 핵심 기술 특허 취득 소식",
    "{c}, 해외 대형 투자 유치 성공",
    "{c}, 신제품 초기 판매 돌풍",
    "{c}, 정부 국책 사업자로 선정",
]
BAD_NEWS = [
    "{c}, 분기 실적 쇼크… 시장 예상치 크게 하회",
    "{c}, 핵심 임원 횡령 의혹 제기",
    "{c}, 주요 공장 가동 중단 사고",
    "{c}, 대형 소송 패소 소식",
    "{c}, 주력 제품 리콜 결정",
    "{c}, 신용등급 하향 조정",
]


def phase_text(phase: int) -> str:
    phase = phase if phase in PHASES else 3
    name, emoji, _ = PHASES[phase]
    drift = cfg.get(f"주식_단계{phase}_수익률")
    vol = cfg.get(f"주식_단계{phase}_변동성")
    return f"{emoji} {name} (일 기대 {drift:+.1f}% · 변동성 {vol:.1f}%)"


def trade_fill(price: float, qty: int, total_shares: int, buy: bool):
    """
    수량만큼 체결할 때 (평균 체결가, 체결 후 가격).
    가격 충격을 지수형으로 적용하고 그 경로의 평균가로 체결하므로
    '사자마자 오른 가격에 되파는' 무한 차익이 생기지 않는다 (왕복 시 수수료만큼 손해).
    """
    x = cfg.get("주식_거래충격") * qty / max(total_shares, 1)
    if x < 1e-9:
        return price, price
    if buy:
        return price * (math.exp(x) - 1) / x, price * math.exp(x)
    return price * (1 - math.exp(-x)) / x, price * math.exp(-x)


class EconomyCog(commands.Cog):
    """경제 전반(물가 · 지갑 · 주식 · 턴) 관리 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tick_count = 0
        self.last_ret: dict[str, float] = {}
        self.daily_turn.start()
        self.market_tick.start()

    async def cog_load(self):
        """설정 로드 + 주식 시장용 컬럼 마이그레이션 (기존 DB 호환)"""
        await cfg.load(self.db)
        async with aiosqlite.connect(self.db) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS stock_history (
                    company_name TEXT, ts INTEGER, price INTEGER
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_stock_history ON stock_history (company_name, ts)")
            cur = await db.execute("PRAGMA table_info(stocks)")
            cols = {r[1] for r in await cur.fetchall()}
            for col, typ in (("exact_price", "REAL"), ("fair_value", "REAL"), ("day_open", "INTEGER")):
                if col not in cols:
                    await db.execute(f"ALTER TABLE stocks ADD COLUMN {col} {typ}")
            await db.execute("UPDATE stocks SET exact_price = current_price WHERE exact_price IS NULL")
            await db.execute("UPDATE stocks SET fair_value = current_price WHERE fair_value IS NULL")
            await db.execute("UPDATE stocks SET day_open = current_price WHERE day_open IS NULL")
            cur = await db.execute("PRAGMA table_info(server_settings)")
            if "stock_channel_id" not in {r[1] for r in await cur.fetchall()}:
                await db.execute("ALTER TABLE server_settings ADD COLUMN stock_channel_id INTEGER")
            await db.commit()

    def cog_unload(self):
        self.daily_turn.cancel()
        self.market_tick.cancel()

    # ── 헬퍼 ─────────────────────────────────────────────────
    @property
    def db(self):
        return self.bot.db_path

    async def _get_inflation(self) -> float:
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT cumulative_inflation FROM server_settings WHERE id = 1"
            )
            row = await cur.fetchone()
            return row[0] if row else 1.0

    # ═════════════════════════════════════════════════════════
    #  증시 채널 설정
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="주식채널설정", description="증시 속보·시황이 올라올 채널을 설정합니다 (관리자)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_stock_channel(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE server_settings SET stock_channel_id = ? WHERE id = 1", (interaction.channel_id,)
            )
            await db.commit()
        await interaction.response.send_message(
            f"📈 증시 채널이 {interaction.channel.mention}(으)로 설정되었습니다! "
            f"뉴스·상한가/하한가 속보와 매시 정각 시황이 올라옵니다."
        )

    async def _stock_channel(self):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT stock_channel_id FROM server_settings WHERE id = 1")
            row = await cur.fetchone()
        return self.bot.get_channel(row[0]) if row and row[0] else None

    # ═════════════════════════════════════════════════════════
    #  복리 물가
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="물가상승", description="복리 물가를 상승시킵니다 (관리자 전용)")
    @app_commands.describe(퍼센트="상승시킬 퍼센트 (예: 5)")
    @app_commands.checks.has_permissions(administrator=True)
    async def inflate(self, interaction: discord.Interaction, 퍼센트: float):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT cumulative_inflation FROM server_settings WHERE id = 1"
            )
            row = await cur.fetchone()
            old = row[0] if row else 1.0
            new = old * (1 + 퍼센트 / 100)
            await db.execute(
                "UPDATE server_settings SET cumulative_inflation = ? WHERE id = 1",
                (new,),
            )
            await db.commit()

        embed = discord.Embed(
            title="📈 물가 상승 적용",
            colour=0xFFD700,
        )
        embed.add_field(name="상승률", value=f"{퍼센트:.2f}%", inline=True)
        embed.add_field(name="이전 누적", value=f"×{old:.4f}", inline=True)
        embed.add_field(name="현재 누적", value=f"×{new:.4f}", inline=True)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  내 지갑
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="내지갑", description="내 자산 현황을 확인합니다")
    async def my_wallet(self, interaction: discord.Interaction):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            # 유저 기본 정보
            cur = await db.execute(
                "SELECT money, salary FROM users WHERE user_id = ?", (uid,)
            )
            user = await cur.fetchone()
            money = user["money"]
            salary = user["salary"]

            # 보유 주식
            cur = await db.execute(
                """
                SELECT sh.company_name, sh.quantity, s.current_price
                FROM stock_holdings sh
                JOIN stocks s ON sh.company_name = s.company_name
                WHERE sh.user_id = ? AND sh.quantity > 0
                """,
                (uid,),
            )
            holdings = await cur.fetchall()

        stock_lines = []
        total_stock_value = 0
        for h in holdings:
            val = h["quantity"] * h["current_price"]
            total_stock_value += val
            stock_lines.append(
                f"• **{h['company_name']}** — {h['quantity']:,}주 "
                f"(평가 {val:,}원)"
            )

        total_assets = money + total_stock_value

        embed = discord.Embed(
            title=f"💰 {interaction.user.display_name}의 지갑",
            colour=0xFFD700,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="💵 현금", value=f"{money:,}원", inline=True)
        embed.add_field(name="💼 연봉", value=f"{salary:,}원", inline=True)
        embed.add_field(
            name="📊 주식 평가액",
            value=f"{total_stock_value:,}원",
            inline=True,
        )
        embed.add_field(
            name="🏦 총 자산",
            value=f"**{total_assets:,}원**",
            inline=False,
        )
        if stock_lines:
            embed.add_field(
                name="📋 보유 주식",
                value="\n".join(stock_lines) or "없음",
                inline=False,
            )
        else:
            embed.add_field(name="📋 보유 주식", value="보유 주식 없음", inline=False)
        embed.set_footer(text="가상 국가 경제 시스템")
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  잔액 설정
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="잔액설정", description="유저의 잔액을 설정합니다 (관리자)")
    @app_commands.describe(유저="대상 유저", 금액="설정할 금액")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_balance(
        self, interaction: discord.Interaction, 유저: discord.Member, 금액: int
    ):
        await self.bot.ensure_user(유저.id)
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE users SET money = ? WHERE user_id = ?", (금액, 유저.id)
            )
            await db.commit()
        embed = discord.Embed(
            title="✅ 잔액 설정 완료",
            description=f"{유저.mention}의 잔액이 **{금액:,}원**으로 설정되었습니다.",
            colour=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  연봉 설정
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="연봉설정", description="유저의 연봉을 설정합니다 (관리자)")
    @app_commands.describe(유저="대상 유저", 금액="설정할 연봉")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_salary(
        self, interaction: discord.Interaction, 유저: discord.Member, 금액: int
    ):
        await self.bot.ensure_user(유저.id)
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE users SET salary = ? WHERE user_id = ?", (금액, 유저.id)
            )
            await db.commit()
        embed = discord.Embed(
            title="✅ 연봉 설정 완료",
            description=f"{유저.mention}의 연봉이 **{금액:,}원**으로 설정되었습니다.",
            colour=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  송금
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="송금", description="다른 유저에게 송금합니다")
    @app_commands.describe(유저="받을 유저", 금액="송금액")
    async def transfer(
        self, interaction: discord.Interaction, 유저: discord.Member, 금액: int
    ):
        sender = interaction.user.id
        receiver = 유저.id
        if 금액 <= 0:
            return await interaction.response.send_message(
                "❌ 송금액은 1원 이상이어야 합니다.", ephemeral=True
            )
        if sender == receiver:
            return await interaction.response.send_message(
                "❌ 자기 자신에게 송금할 수 없습니다.", ephemeral=True
            )
        await self.bot.ensure_user(sender)
        await self.bot.ensure_user(receiver)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT money FROM users WHERE user_id = ?", (sender,)
            )
            row = await cur.fetchone()
            if row[0] < 금액:
                return await interaction.response.send_message(
                    "❌ 잔액이 부족합니다.", ephemeral=True
                )
            await db.execute(
                "UPDATE users SET money = money - ? WHERE user_id = ?",
                (금액, sender),
            )
            await db.execute(
                "UPDATE users SET money = money + ? WHERE user_id = ?",
                (금액, receiver),
            )
            await db.commit()

        embed = discord.Embed(
            title="💸 송금 완료",
            description=(
                f"{interaction.user.mention} → {유저.mention}\n"
                f"**{금액:,}원** 송금되었습니다."
            ),
            colour=0x3498DB,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  기업 배정 (관리자 전용)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="기업배정", description="유저에게 기업을 상장시켜 배정합니다 (관리자)")
    @app_commands.describe(
        유저="기업을 배정받을 유저",
        기업명="기업 이름",
        섹터명="섹터 (에너지화학/소재/산업재/모빌리티/정보기술/금융 및 부동산/소비재/헬스케어/미디어 및 콘텐츠)",
        초기주가="1주당 초기 가격",
        발행주식수="총 발행 주식 수",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def assign_company(
        self,
        interaction: discord.Interaction,
        유저: discord.Member,
        기업명: str,
        섹터명: str,
        초기주가: int,
        발행주식수: int,
    ):
        if 초기주가 <= 0 or 발행주식수 <= 0:
            return await interaction.response.send_message(
                "❌ 초기주가와 발행주식수는 1 이상이어야 합니다.", ephemeral=True
            )
        if 섹터명 not in VALID_SECTORS:
            return await interaction.response.send_message(
                f"❌ 유효하지 않은 섹터입니다.\n허용 섹터: {', '.join(VALID_SECTORS)}",
                ephemeral=True,
            )
        uid = 유저.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT company_name FROM stocks WHERE company_name = ?", (기업명,))
            if await cur.fetchone():
                return await interaction.response.send_message("❌ 이미 같은 이름의 기업이 존재합니다.", ephemeral=True)

            await db.execute(
                """INSERT INTO stocks (company_name, owner_id, sector_name, current_price, total_shares,
                                      economic_phase, exact_price, fair_value, day_open)
                   VALUES (?, ?, ?, ?, ?, 3, ?, ?, ?)""",
                (기업명, uid, 섹터명, 초기주가, 발행주식수, 초기주가, 초기주가, 초기주가),
            )
            owner_qty = round(발행주식수 * cfg.get("주식_대주주지분"))
            if owner_qty > 0:
                await db.execute(
                    """INSERT OR REPLACE INTO stock_holdings (user_id, company_name, quantity) VALUES (?, ?, ?)""",
                    (uid, 기업명, owner_qty),
                )
            await db.commit()

        market_cap = 초기주가 * 발행주식수
        embed = discord.Embed(
            title="🔔 신규 기업 상장 및 배정!",
            description=f"**{기업명}** 이(가) {섹터명} 섹터에 상장되어 {유저.mention}님에게 배정되었습니다!",
            colour=0xE67E22,
        )
        embed.add_field(name="초기 주가", value=f"{초기주가:,}원", inline=True)
        embed.add_field(name="발행 주식 수", value=f"{발행주식수:,}주", inline=True)
        embed.add_field(name="시가총액", value=f"{market_cap:,}원", inline=True)
        embed.add_field(name="대주주 지분", value=f"{owner_qty:,}주 ({owner_qty / 발행주식수 * 100:.0f}%)", inline=True)
        embed.add_field(name="시장 유통 물량", value=f"{발행주식수 - owner_qty:,}주", inline=True)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  유통 물량 조정 (관리자 전용)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="대주주지분설정", description="대주주 보유량을 조정해 시장 유통 물량을 풀거나 거둡니다 (관리자)")
    @app_commands.describe(기업명="대상 기업", 지분퍼센트="대주주 지분 % (예: 40 → 나머지 중 미보유분이 시장 물량)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_owner_stake(self, interaction: discord.Interaction, 기업명: str, 지분퍼센트: float):
        if not 0 <= 지분퍼센트 <= 100:
            return await interaction.response.send_message("❌ 0~100 사이로 입력하세요.", ephemeral=True)
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT owner_id, total_shares FROM stocks WHERE company_name = ?", (기업명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            owner, total = row
            cur = await db.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM stock_holdings WHERE company_name = ? AND user_id != ?",
                (기업명, owner))
            others = (await cur.fetchone())[0]
            target = round(total * 지분퍼센트 / 100)
            if target > total - others:
                return await interaction.response.send_message(
                    f"❌ 다른 주주가 {others:,}주를 보유 중이라 대주주는 최대 {total - others:,}주"
                    f"({(total - others) / total * 100:.1f}%)까지만 가질 수 있습니다.", ephemeral=True)
            await db.execute("DELETE FROM stock_holdings WHERE company_name = ? AND user_id = ?", (기업명, owner))
            if target > 0:
                await db.execute("INSERT INTO stock_holdings (user_id, company_name, quantity) VALUES (?, ?, ?)",
                                 (owner, 기업명, target))
            await db.commit()
        await interaction.response.send_message(
            f"🏦 **{기업명}** 대주주 <@{owner}> 지분 → **{target:,}주** ({지분퍼센트:g}%)\n"
            f"시장 유통 물량: **{total - others - target:,}주** · 다른 주주 보유 {others:,}주")

    # ═════════════════════════════════════════════════════════
    #  기업 삭제 (관리자 전용)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="기업삭제", description="기업을 시장에서 상장 폐지(삭제)합니다 (관리자)")
    @app_commands.describe(기업명="삭제할 기업 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_company(self, interaction: discord.Interaction, 기업명: str):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT company_name FROM stocks WHERE company_name = ?", (기업명,))
            if not await cur.fetchone():
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            
            await db.execute("DELETE FROM stocks WHERE company_name = ?", (기업명,))
            await db.execute("DELETE FROM stock_holdings WHERE company_name = ?", (기업명,))
            await db.execute("DELETE FROM stock_history WHERE company_name = ?", (기업명,))
            await db.commit()

        embed = discord.Embed(
            title="🗑️ 기업 상장 폐지 완료",
            description=f"**{기업명}**이(가) 주식 시장에서 삭제되었습니다.",
            colour=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  주가 강제 조정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="주가강제조정", description="특정 기업 주가를 강제 변경합니다 (관리자)")
    @app_commands.describe(기업명="대상 기업", 새로운주가="새로 설정할 주가")
    @app_commands.checks.has_permissions(administrator=True)
    async def force_price(
        self, interaction: discord.Interaction, 기업명: str, 새로운주가: int
    ):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT current_price FROM stocks WHERE company_name = ?", (기업명,)
            )
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message(
                    "❌ 해당 기업이 존재하지 않습니다.", ephemeral=True
                )
            old_price = row[0]
            새로운주가 = max(1, 새로운주가)
            await db.execute(
                """UPDATE stocks SET current_price = ?, exact_price = ?, fair_value = ?, day_open = ?
                   WHERE company_name = ?""",
                (새로운주가, 새로운주가, 새로운주가, 새로운주가, 기업명),
            )
            await db.commit()

        embed = discord.Embed(
            title="⚙️ 주가 강제 조정",
            description=f"**{기업명}** 주가가 변경되었습니다.",
            colour=0xE74C3C,
        )
        embed.add_field(name="이전 주가", value=f"{old_price:,}원", inline=True)
        embed.add_field(name="변경 주가", value=f"{새로운주가:,}원", inline=True)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  기업 상황(경기단계) 설정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="기업상황설정", description="기업별 경기 단계를 설정합니다 (관리자)")
    @app_commands.describe(기업명="대상 기업", 경기단계1_5="1~5 사이의 경기 단계")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_phase(
        self, interaction: discord.Interaction, 기업명: str, 경기단계1_5: int
    ):
        if 경기단계1_5 not in range(1, 6):
            return await interaction.response.send_message(
                "❌ 경기 단계는 1~5 사이여야 합니다.", ephemeral=True
            )
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT company_name FROM stocks WHERE company_name = ?", (기업명,)
            )
            if not await cur.fetchone():
                return await interaction.response.send_message(
                    "❌ 해당 기업이 존재하지 않습니다.", ephemeral=True
                )
            await db.execute(
                "UPDATE stocks SET economic_phase = ? WHERE company_name = ?",
                (경기단계1_5, 기업명),
            )
            await db.commit()

        embed = discord.Embed(
            title="🏢 기업 상황 설정 완료",
            description=f"**{기업명}** → {phase_text(경기단계1_5)}",
            colour=0x9B59B6,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  기업 랭킹
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="기업랭킹", description="시가총액 기준 기업 순위를 확인합니다")
    async def company_ranking(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT company_name, sector_name, current_price, total_shares,
                          current_price * total_shares AS market_cap, economic_phase
                   FROM stocks ORDER BY market_cap DESC"""
            )
            rows = await cur.fetchall()

        if not rows:
            return await interaction.response.send_message(
                "📭 상장된 기업이 없습니다.", ephemeral=True
            )

        lines = []
        for i, r in enumerate(rows, 1):
            pe = PHASES.get(r["economic_phase"], PHASES[3])[1]
            lines.append(
                f"**{i}. {r['company_name']}** [{r['sector_name']}] {pe}\n"
                f"   주가 {r['current_price']:,}원 · "
                f"발행 {r['total_shares']:,}주 · "
                f"시총 **{r['market_cap']:,}원**"
            )

        embed = discord.Embed(
            title="🏆 기업 시가총액 랭킹",
            description="\n\n".join(lines),
            colour=0xF1C40F,
        )
        embed.set_footer(text=f"총 {len(rows)}개 기업")
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  주가 확인
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="주가확인", description="특정 기업이나 전체 주식 시장의 현재 주가를 확인합니다")
    @app_commands.describe(기업명="확인할 기업 이름 (선택사항)")
    async def check_price(self, interaction: discord.Interaction, 기업명: str = None):
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            if 기업명:
                cur = await db.execute("SELECT * FROM stocks WHERE company_name = ?", (기업명,))
                row = await cur.fetchone()
                if not row:
                    return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
                since = int(time.time()) - 86400
                cur = await db.execute(
                    "SELECT price FROM stock_history WHERE company_name = ? AND ts >= ? ORDER BY ts",
                    (기업명, since),
                )
                hist = [r["price"] for r in await cur.fetchall()]
                available = await float_available(db, 기업명)

            else:
                cur = await db.execute("SELECT * FROM stocks ORDER BY current_price * total_shares DESC")
                rows = await cur.fetchall()

        if 기업명:
            price = row["current_price"]
            open_ = row["day_open"] or price
            market_cap = price * row["total_shares"]
            hist = hist + [price]
            embed = discord.Embed(
                title=f"🏢 {row['company_name']} 주가 정보",
                description=f"## {price:,}원 {change_str(price, open_)}\n`{sparkline(hist)}`",
                colour=0xE74C3C if price >= open_ else 0x3498DB,
            )
            lo_lim, hi_lim = limit_band(open_)
            embed.add_field(name="오늘 고가 / 저가", value=f"{max(hist):,} / {min(hist):,}원", inline=True)
            embed.add_field(name="기준가 (상/하한가)", value=f"{open_:,}원 ({hi_lim:,} / {lo_lim:,})", inline=True)
            embed.add_field(name="시가총액", value=f"{market_cap:,}원", inline=True)
            embed.add_field(name="발행 주식 수", value=f"{row['total_shares']:,}주", inline=True)
            embed.add_field(name="시장 잔여 물량", value=f"{available:,}주", inline=True)
            embed.add_field(name="섹터", value=row["sector_name"], inline=True)
            embed.add_field(name="경기 상황", value=phase_text(row["economic_phase"]), inline=False)
            embed.set_footer(text=f"최근 24시간 · {cfg.fmt(cfg.get('주식_틱간격분'))}분마다 시세 변동")
            return await interaction.response.send_message(embed=embed)

        if not rows:
            return await interaction.response.send_message("📭 상장된 기업이 없습니다.", ephemeral=True)
        lines = []
        for r in rows[:30]:
            pe = PHASES.get(r["economic_phase"], PHASES[3])[1]
            lines.append(
                f"{pe} **{r['company_name']}** {r['current_price']:,}원 "
                f"{change_str(r['current_price'], r['day_open'] or r['current_price'])}"
            )
        embed = discord.Embed(
            title="📈 전체 주식 시장 현황",
            description="\n".join(lines),
            colour=0x3498DB,
        )
        embed.set_footer(text=f"종합지수 {market_index(rows):.2f} · 등락률은 오늘 기준가 대비")
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  매수 / 매도
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="매수", description="주식을 매수합니다")
    @app_commands.describe(기업명="매수할 기업", 수량="매수할 주식 수")
    async def buy_stock(self, interaction: discord.Interaction, 기업명: str, 수량: int):
        await self._trade(interaction, 기업명, 수량, buy=True)

    @app_commands.command(name="매도", description="주식을 매도합니다")
    @app_commands.describe(기업명="매도할 기업", 수량="매도할 주식 수")
    async def sell_stock(self, interaction: discord.Interaction, 기업명: str, 수량: int):
        await self._trade(interaction, 기업명, 수량, buy=False)

    async def _trade(self, interaction, company, qty, buy):
        if qty <= 0:
            return await interaction.response.send_message("❌ 1주 이상 거래해야 합니다.", ephemeral=True)
        uid = interaction.user.id
        await self.bot.ensure_user(uid)
        fee_rate = cfg.get("주식_수수료")

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT exact_price, total_shares, day_open FROM stocks WHERE company_name = ?", (company,)
            )
            stock = await cur.fetchone()
            if not stock:
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            price, total_shares, day_open = stock
            if buy:
                available = await float_available(db, company)
                if qty > available:
                    return await interaction.response.send_message(
                        f"❌ 시장에 남은 물량이 부족합니다. 매수 가능: **{available:,}주** "
                        f"(발행 {total_shares:,}주 중 나머지는 다른 주주 보유)", ephemeral=True)
            lo_lim, hi_lim = limit_band(day_open or price)
            if buy and price >= hi_lim:
                return await interaction.response.send_message(
                    "🔒 상한가에 도달해 오늘은 더 이상 매수할 수 없습니다.", ephemeral=True)
            if not buy and price <= lo_lim:
                return await interaction.response.send_message(
                    "🔒 하한가에 도달해 오늘은 더 이상 매도할 수 없습니다.", ephemeral=True)

            avg, new_price = trade_fill(price, qty, total_shares, buy)
            new_price = min(max(new_price, lo_lim), hi_lim)
            gross = avg * qty
            fee = math.ceil(gross * fee_rate)

            if buy:
                cost = math.ceil(gross) + fee
                cur = await db.execute("SELECT money FROM users WHERE user_id = ?", (uid,))
                wallet = (await cur.fetchone())[0]
                if wallet < cost:
                    return await interaction.response.send_message(
                        f"❌ 잔액 부족! 필요: {cost:,}원 / 보유: {wallet:,}원", ephemeral=True)
                await db.execute("UPDATE users SET money = money - ? WHERE user_id = ?", (cost, uid))
                await db.execute(
                    """INSERT INTO stock_holdings (user_id, company_name, quantity) VALUES (?, ?, ?)
                       ON CONFLICT(user_id, company_name) DO UPDATE SET quantity = quantity + ?""",
                    (uid, company, qty, qty),
                )
                amount = cost
            else:
                cur = await db.execute(
                    "SELECT quantity FROM stock_holdings WHERE user_id = ? AND company_name = ?", (uid, company)
                )
                row = await cur.fetchone()
                owned = row[0] if row else 0
                if owned < qty:
                    return await interaction.response.send_message(
                        f"❌ 보유 주식 부족! 보유: {owned:,}주", ephemeral=True)
                amount = max(0, math.floor(gross) - fee)
                await db.execute("UPDATE users SET money = money + ? WHERE user_id = ?", (amount, uid))
                await db.execute(
                    "UPDATE stock_holdings SET quantity = quantity - ? WHERE user_id = ? AND company_name = ?",
                    (qty, uid, company),
                )
                await db.execute(
                    "DELETE FROM stock_holdings WHERE user_id = ? AND company_name = ? AND quantity <= 0",
                    (uid, company),
                )
            await set_price(db, company, new_price)
            await db.commit()

        shown_new = max(1, round(new_price))
        embed = discord.Embed(
            title="📈 매수 체결" if buy else "📉 매도 체결",
            description=(
                f"**{company}** {qty:,}주 {'매수' if buy else '매도'} 완료\n"
                f"평균 체결가: {avg:,.0f}원 · 수수료: {fee:,}원\n"
                f"{'총 지출' if buy else '실수령액'}: **{amount:,}원**\n"
                f"*(거래 영향으로 주가 {round(price):,} → {shown_new:,}원)*"
            ),
            colour=0xE74C3C if buy else 0x3498DB,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  시세 변동 (틱마다 자동 실행)
    # ═════════════════════════════════════════════════════════
    @tasks.loop(minutes=10)
    async def market_tick(self):
        interval = cfg.get("주식_틱간격분")
        tpd = 1440 / interval
        vol_mult = cfg.get("주식_변동성배율")
        w_mkt, w_sec = cfg.get("주식_시장연동"), cfg.get("주식_섹터연동")
        w_idio = math.sqrt(max(0.0, 1 - w_mkt ** 2 - w_sec ** 2))
        revert = cfg.get("주식_평균회귀")
        momentum = cfg.get("주식_모멘텀")
        news_p = cfg.get("주식_뉴스빈도") / tpd
        news_mult = cfg.get("주식_뉴스강도")
        event_p = cfg.get("주식_시장이벤트빈도") / tpd

        alerts: list[str] = []
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM stocks")
            rows = await cur.fetchall()
            if not rows:
                return

            # 시장 전체 / 섹터 공통 충격
            market_z = random.gauss(0, 1)
            sector_z = {s: random.gauss(0, 1) for s in {r["sector_name"] for r in rows}}
            event = None
            if random.random() < event_p:
                event = random_market_event({r["sector_name"] for r in rows})
                alerts.append(event["text"])

            now = int(time.time())
            for r in rows:
                name = r["company_name"]
                phase = r["economic_phase"] if r["economic_phase"] in PHASES else 3
                drift = cfg.get(f"주식_단계{phase}_수익률") / 100 / tpd
                vol = cfg.get(f"주식_단계{phase}_변동성") / 100 / math.sqrt(tpd) * vol_mult
                price = r["exact_price"] or float(r["current_price"])
                fair = r["fair_value"] or price
                open_ = r["day_open"] or r["current_price"]

                z = w_mkt * market_z + w_sec * sector_z[r["sector_name"]] + w_idio * random.gauss(0, 1)
                if random.random() < 0.03:
                    z *= 2.5  # 가끔 튀는 날 (두꺼운 꼬리)
                prev_ret = self.last_ret.get(name, 0.0)
                ret = drift + revert * math.log(fair / price) + momentum * prev_ret + vol * z
                fair *= math.exp(drift + vol * 0.3 * random.gauss(0, 1))

                # 시장/섹터 이벤트
                if event and (event["sector"] is None or event["sector"] == r["sector_name"]):
                    jump = event["pct"] / 100
                    ret += jump
                    fair *= math.exp(jump * 0.8)

                # 개별 기업 뉴스
                if random.random() < news_p:
                    good = random.random() < PHASES[phase][2]
                    pct = random.uniform(3, 12) * news_mult * (1 if good else -1)
                    ret += pct / 100
                    fair *= math.exp(pct / 100 * 0.7)
                    tmpl = random.choice(GOOD_NEWS if good else BAD_NEWS)
                    alerts.append(f"{'🟥 [호재]' if good else '🟦 [악재]'} {tmpl.format(c=f'**{name}**')} ({pct:+.1f}%)")

                old_int = r["current_price"]
                lo_lim, hi_lim = limit_band(open_)
                new_price = min(max(price * math.exp(ret), lo_lim), hi_lim)
                self.last_ret[name] = math.log(new_price / price)
                new_int = max(1, round(new_price))
                if new_int >= hi_lim and old_int < hi_lim:
                    alerts.append(f"🚀 **{name}** 상한가 직행!! {new_int:,}원 ({change_str(new_int, open_)})")
                elif new_int <= lo_lim and old_int > lo_lim:
                    alerts.append(f"🧊 **{name}** 하한가 추락… {new_int:,}원 ({change_str(new_int, open_)})")

                await db.execute(
                    "UPDATE stocks SET exact_price = ?, current_price = ?, fair_value = ? WHERE company_name = ?",
                    (new_price, new_int, fair, name),
                )
                await db.execute(
                    "INSERT INTO stock_history (company_name, ts, price) VALUES (?, ?, ?)", (name, now, new_int)
                )
            await db.execute("DELETE FROM stock_history WHERE ts < ?", (now - 3 * 86400,))
            await db.commit()

        self.tick_count += 1
        ch = await self._stock_channel()
        if ch:
            for a in alerts:
                await ch.send(a)
            if self.tick_count % max(1, round(60 / interval)) == 0:
                await self._send_market_report(ch)

    @market_tick.before_loop
    async def before_market_tick(self):
        await self.bot.wait_until_ready()
        await cfg.load(self.db)
        self.market_tick.change_interval(minutes=cfg.get("주식_틱간격분"))

    @commands.Cog.listener()
    async def on_config_changed(self, key: str):
        if key in ("주식_틱간격분", None):
            self.market_tick.change_interval(minutes=cfg.get("주식_틱간격분"))

    async def _send_market_report(self, ch):
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM stocks")
            rows = await cur.fetchall()
        if not rows:
            return
        chg = lambda r: r["current_price"] / max(r["day_open"] or r["current_price"], 1) - 1
        ranked = sorted(rows, key=chg, reverse=True)
        idx = market_index(rows)
        embed = discord.Embed(
            title=f"🕐 {datetime.now(KST).strftime('%H시')} 정각 시황",
            description=f"종합지수 **{idx:.2f}** ({(idx / 1000 - 1) * 100:+.2f}%)",
            colour=0xE74C3C if idx >= 1000 else 0x3498DB,
        )
        fmt = lambda r: f"**{r['company_name']}** {r['current_price']:,}원 {change_str(r['current_price'], r['day_open'] or r['current_price'])}"
        embed.add_field(name="🔺 상승 TOP", value="\n".join(fmt(r) for r in ranked[:3]), inline=True)
        embed.add_field(name="🔻 하락 TOP", value="\n".join(fmt(r) for r in ranked[::-1][:3]), inline=True)
        await ch.send(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  관리자 — 시장 개입 도구
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="뉴스발생", description="특정 기업에 호재/악재 뉴스를 터뜨립니다 (관리자)")
    @app_commands.describe(기업명="대상 기업", 변동퍼센트="주가 영향 % (예: 8 또는 -12)", 내용="뉴스 제목 (비우면 자동 생성)",
                           지속="true면 적정가도 같이 이동(장기 영향), false면 일시적 충격")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_news(self, interaction: discord.Interaction, 기업명: str, 변동퍼센트: float,
                         내용: str = None, 지속: bool = True):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT exact_price, fair_value, day_open FROM stocks WHERE company_name = ?", (기업명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            price, fair, open_ = row
            factor = math.exp(변동퍼센트 / 100)
            lo_lim, hi_lim = limit_band(open_ or price)
            new_price = min(max(price * factor, lo_lim), hi_lim)
            await set_price(db, 기업명, new_price, fair * factor if 지속 else None)
            await db.commit()
        good = 변동퍼센트 >= 0
        title = 내용 or random.choice(GOOD_NEWS if good else BAD_NEWS).format(c=기업명)
        text = (f"{'🟥 [속보·호재]' if good else '🟦 [속보·악재]'} {title}\n"
                f"**{기업명}** {round(price):,} → **{round(new_price):,}원** ({변동퍼센트:+.1f}%)")
        await interaction.response.send_message(text)
        ch = await self._stock_channel()
        if ch and ch.id != interaction.channel_id:
            await ch.send(text)

    @app_commands.command(name="시장이벤트", description="시장 전체 또는 특정 섹터를 한 번에 움직입니다 (관리자)")
    @app_commands.describe(변동퍼센트="주가 영향 % (예: -5)", 내용="이벤트 설명 (예: 기준금리 인상)",
                           섹터="특정 섹터만 (비우면 전체 시장)")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_market_event(self, interaction: discord.Interaction, 변동퍼센트: float, 내용: str,
                                 섹터: str = None):
        if 섹터 and 섹터 not in VALID_SECTORS:
            return await interaction.response.send_message(
                f"❌ 유효하지 않은 섹터입니다.\n허용 섹터: {', '.join(VALID_SECTORS)}", ephemeral=True)
        factor = math.exp(변동퍼센트 / 100)
        async with aiosqlite.connect(self.db) as db:
            q = "SELECT company_name, exact_price, fair_value, day_open, current_price FROM stocks"
            cur = await db.execute(q + (" WHERE sector_name = ?" if 섹터 else ""), (섹터,) if 섹터 else ())
            rows = await cur.fetchall()
            for name, price, fair, open_, cur_int in rows:
                price = price or cur_int
                lo_lim, hi_lim = limit_band(open_ or cur_int)
                await set_price(db, name, min(max(price * factor, lo_lim), hi_lim), (fair or price) * factor)
            await db.commit()
        text = (f"{'📢🔴' if 변동퍼센트 >= 0 else '📢🔵'} **[{섹터 or '시장 전체'}] {내용}**\n"
                f"{len(rows)}개 종목 일제히 {변동퍼센트:+.1f}%!")
        await interaction.response.send_message(text)
        ch = await self._stock_channel()
        if ch and ch.id != interaction.channel_id:
            await ch.send(text)

    @app_commands.command(name="섹터상황설정", description="섹터 전체 기업의 경기 단계를 한 번에 바꿉니다 (관리자)")
    @app_commands.describe(섹터="대상 섹터", 경기단계1_5="1 불황 · 2 회복기 · 3 보통 · 4 경기과열 · 5 공황")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_sector_phase(self, interaction: discord.Interaction, 섹터: str, 경기단계1_5: int):
        if 섹터 not in VALID_SECTORS or 경기단계1_5 not in PHASES:
            return await interaction.response.send_message("❌ 섹터 이름 또는 단계(1~5)가 올바르지 않습니다.", ephemeral=True)
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "UPDATE stocks SET economic_phase = ? WHERE sector_name = ?", (경기단계1_5, 섹터))
            n = cur.rowcount
            await db.commit()
        await interaction.response.send_message(f"🏭 **{섹터}** 섹터 {n}개 기업 → {phase_text(경기단계1_5)}")

    @app_commands.command(name="적정가설정", description="기업의 적정가(주가가 서서히 수렴할 목표)를 설정합니다 (관리자)")
    @app_commands.describe(기업명="대상 기업", 적정가="목표 가격")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_fair_value(self, interaction: discord.Interaction, 기업명: str, 적정가: int):
        if 적정가 <= 0:
            return await interaction.response.send_message("❌ 적정가는 1 이상이어야 합니다.", ephemeral=True)
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("UPDATE stocks SET fair_value = ? WHERE company_name = ?", (적정가, 기업명))
            if cur.rowcount == 0:
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            await db.commit()
        half_life = math.log(2) / max(cfg.get("주식_평균회귀"), 1e-6) * cfg.get("주식_틱간격분") / 60
        await interaction.response.send_message(
            f"🎯 **{기업명}** 적정가 → **{적정가:,}원**. 주가가 서서히 수렴합니다 (격차 반감기 약 {half_life:.1f}시간)."
        )

    @app_commands.command(name="유상증자", description="신주를 발행해 대주주에게 지급합니다 — 주가는 희석됩니다 (관리자)")
    @app_commands.describe(기업명="대상 기업", 발행수량="새로 발행할 주식 수",
                           시장공급="true면 신주를 대주주 대신 시장 유통 물량으로 풉니다")
    @app_commands.checks.has_permissions(administrator=True)
    async def issue_shares(self, interaction: discord.Interaction, 기업명: str, 발행수량: int,
                           시장공급: bool = False):
        if 발행수량 <= 0:
            return await interaction.response.send_message("❌ 1주 이상 발행해야 합니다.", ephemeral=True)
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT owner_id, total_shares, exact_price, fair_value FROM stocks WHERE company_name = ?", (기업명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            owner, total, price, fair = row
            dilution = total / (total + 발행수량)
            await db.execute("UPDATE stocks SET total_shares = ? WHERE company_name = ?", (total + 발행수량, 기업명))
            await set_price(db, 기업명, price * dilution, fair * dilution, reset_open=True)
            if not 시장공급:
                await db.execute(
                    """INSERT INTO stock_holdings (user_id, company_name, quantity) VALUES (?, ?, ?)
                       ON CONFLICT(user_id, company_name) DO UPDATE SET quantity = quantity + ?""",
                    (owner, 기업명, 발행수량, 발행수량),
                )
            await db.commit()
        where = "시장 유통 물량으로 공급" if 시장공급 else f"<@{owner}>에게 지급"
        await interaction.response.send_message(
            f"🧾 **{기업명}** 유상증자 {발행수량:,}주 → 총 {total + 발행수량:,}주. "
            f"주가 희석 {round(price):,} → **{round(price * dilution):,}원** (신주는 {where})"
        )

    @app_commands.command(name="주식지급", description="시장 물량에서 유저에게 주식을 지급하거나 회수합니다 (관리자)")
    @app_commands.describe(유저="대상 유저", 기업명="기업", 수량="지급 수량 (음수면 회수)")
    @app_commands.checks.has_permissions(administrator=True)
    async def grant_shares(self, interaction: discord.Interaction, 유저: discord.Member, 기업명: str, 수량: int):
        await self.bot.ensure_user(유저.id)
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT 1 FROM stocks WHERE company_name = ?", (기업명,))
            if not await cur.fetchone():
                return await interaction.response.send_message("❌ 해당 기업이 존재하지 않습니다.", ephemeral=True)
            available = await float_available(db, 기업명)
            if 수량 > available:
                return await interaction.response.send_message(
                    f"❌ 시장 잔여 물량({available:,}주)보다 많이 지급할 수 없습니다. "
                    f"`/유상증자` 또는 `/대주주지분설정`으로 물량을 늘리세요.", ephemeral=True)
            await db.execute(
                """INSERT INTO stock_holdings (user_id, company_name, quantity) VALUES (?, ?, MAX(?, 0))
                   ON CONFLICT(user_id, company_name) DO UPDATE SET quantity = MAX(quantity + ?, 0)""",
                (유저.id, 기업명, 수량, 수량),
            )
            await db.execute("DELETE FROM stock_holdings WHERE quantity <= 0")
            cur = await db.execute(
                "SELECT quantity FROM stock_holdings WHERE user_id = ? AND company_name = ?", (유저.id, 기업명))
            row = await cur.fetchone()
            await db.commit()
        await interaction.response.send_message(
            f"✅ {유저.mention} **{기업명}** {수량:+,}주 → 보유 {(row[0] if row else 0):,}주")

    # ═════════════════════════════════════════════════════════
    #  턴 넘기기 (매일 00시 자동 실행)
    # ═════════════════════════════════════════════════════════
    @tasks.loop(time=time_of_day(hour=0, minute=0, tzinfo=KST))
    async def daily_turn(self):
        print("[ECONOMY] 00시 정각 — 연봉 차감 · 배당 · 기준가 갱신 · 스포츠 시즌 마감")
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row

            # ── 선수 연봉(몸값) 차감 로직 ──
            cur = await db.execute("SELECT cumulative_inflation FROM server_settings WHERE id = 1")
            row = await cur.fetchone()
            inf = row["cumulative_inflation"] if row else 1.0

            for table in ("soccer_players", "baseball_players"):
                cur = await db.execute(f'''
                    SELECT st.owner_id, sum(p.base_transfer_fee) as total_cost
                    FROM {table} p
                    JOIN sports_teams st ON p.team_name = st.team_name
                    WHERE p.team_name != '무소속'
                    GROUP BY st.owner_id
                ''')
                for owner in await cur.fetchall():
                    cost = int(owner["total_cost"] * inf)
                    await db.execute("UPDATE users SET money = money - ? WHERE user_id = ?", (cost, owner["owner_id"]))

            # ── 배당 ──
            rate = cfg.get("주식_배당률") / 100
            if rate > 0:
                cur = await db.execute("""
                    SELECT sh.user_id, SUM(sh.quantity * s.current_price) AS value
                    FROM stock_holdings sh JOIN stocks s ON sh.company_name = s.company_name
                    GROUP BY sh.user_id
                """)
                for h in await cur.fetchall():
                    await db.execute("UPDATE users SET money = money + ? WHERE user_id = ?",
                                     (int(h["value"] * rate), h["user_id"]))

            # ── 오늘 종가 → 내일 기준가 (상·하한가 기준) ──
            await db.execute("UPDATE stocks SET day_open = current_price")
            await db.commit()

        self.bot.dispatch("turn_passed")

    @daily_turn.before_loop
    async def before_daily_turn(self):
        await self.bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════
#  시장 유틸
# ═══════════════════════════════════════════════════════════════
async def set_price(db, company, new_price, fair=None, reset_open=False):
    new_int = max(1, round(new_price))
    sets = ["exact_price = ?", "current_price = ?"]
    vals = [new_price, new_int]
    if fair is not None:
        sets.append("fair_value = ?")
        vals.append(fair)
    if reset_open:
        sets.append("day_open = ?")
        vals.append(new_int)
    await db.execute(f"UPDATE stocks SET {', '.join(sets)} WHERE company_name = ?", (*vals, company))


async def float_available(db, company):
    """시장에 남아 있는 매수 가능 물량 = 발행주식수 − 전체 보유량"""
    cur = await db.execute(
        """SELECT s.total_shares - COALESCE((SELECT SUM(quantity) FROM stock_holdings h
                                              WHERE h.company_name = s.company_name), 0)
           FROM stocks s WHERE s.company_name = ?""", (company,))
    row = await cur.fetchone()
    return max(0, row[0]) if row else 0


def limit_band(open_price):
    lim = cfg.get("주식_가격제한폭")
    return max(1, math.ceil(open_price * (1 - lim))), max(1, math.floor(open_price * (1 + lim)))


def change_str(price, base):
    if not base:
        return ""
    pct = (price / base - 1) * 100
    if abs(pct) < 0.005:
        return "(0.00%)"
    return f"{'🔺' if pct > 0 else '🔻'}{abs(pct):.2f}%"


def sparkline(values, width=24):
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)] + [values[-1]]
    lo, hi = min(values), max(values)
    bars = "▁▂▃▄▅▆▇█"
    if hi == lo:
        return bars[3] * len(values)
    return "".join(bars[int((v - lo) / (hi - lo) * 7)] for v in values)


def market_index(rows):
    """종합지수 = 오늘 기준가 시가총액 대비 현재 시가총액 × 1000"""
    base = sum((r["day_open"] or r["current_price"]) * r["total_shares"] for r in rows)
    now = sum(r["current_price"] * r["total_shares"] for r in rows)
    return 1000 * now / base if base else 1000.0


# 호재/악재를 같은 크기로 짝지어 둠 → 장기적으로 한쪽으로 쏠리지 않음
MARKET_EVENTS = [
    (None, 3.0, "🏦 중앙은행 기준금리 전격 인하!"),
    (None, -3.0, "🏦 중앙은행 기준금리 기습 인상!"),
    (None, 2.5, "🌏 해외 증시 훈풍, 외국인 대규모 순매수 유입!"),
    (None, -2.5, "🌪️ 해외발 금융 불안, 외국인 대량 매도!"),
    (None, 4.0, "💵 정부, 대규모 경기부양책 발표!"),
    (None, -4.0, "⚠️ 지정학적 긴장 고조… 투자심리 급랭!"),
    ("에너지화학", 4.0, "🛢️ 국제 유가 급등!"),
    ("에너지화학", -4.0, "🛢️ 국제 유가 폭락!"),
    ("정보기술", 5.0, "💾 반도체 슈퍼사이클 도래 전망!"),
    ("정보기술", -5.0, "💾 반도체 재고 과잉 우려!"),
    ("모빌리티", 4.0, "🚗 친환경차 보조금 대폭 확대!"),
    ("모빌리티", -4.0, "🚗 친환경차 보조금 조기 종료!"),
    ("헬스케어", 6.0, "💊 신약 규제 완화 발표!"),
    ("헬스케어", -6.0, "💊 임상 실패 소식에 업종 전반 충격!"),
    ("금융 및 부동산", 3.5, "🏠 부동산 규제 완화 발표!"),
    ("금융 및 부동산", -3.5, "🏠 대출 규제 강화 발표!"),
    ("소비재", 3.0, "🛍️ 소비심리지수 역대 최고!"),
    ("소비재", -3.0, "🛍️ 소비심리 급랭, 지갑 닫는 소비자들!"),
    ("미디어 및 콘텐츠", 5.0, "🎬 자국 콘텐츠 글로벌 흥행 돌풍!"),
    ("미디어 및 콘텐츠", -5.0, "🎬 기대작 연이은 흥행 참패!"),
    ("소재", 3.5, "⛏️ 원자재 가격 반등!"),
    ("소재", -3.5, "⛏️ 원자재 수요 급감!"),
    ("산업재", 3.5, "🏗️ 대규모 SOC 인프라 투자 발표!"),
    ("산업재", -3.5, "🏗️ 인프라 예산 대폭 삭감!"),
]


def random_market_event(sectors):
    pool = [e for e in MARKET_EVENTS if e[0] is None or e[0] in sectors]
    sector, pct, text = random.choice(pool)
    pct *= random.uniform(0.6, 1.4) * cfg.get("주식_뉴스강도")
    where = sector or "시장 전체"
    return {"sector": sector, "pct": pct,
            "text": f"📢 **[{where}] {text}** 관련 종목 {pct:+.1f}% 영향!"}


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
