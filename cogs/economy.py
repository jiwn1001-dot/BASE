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
from datetime import time, timezone, timedelta

# ─── 허용 섹터 ────────────────────────────────────────────────
VALID_SECTORS = [
    "에너지화학", "소재", "산업재", "모빌리티",
    "정보기술", "금융 및 부동산", "소비재", "헬스케어",
    "미디어 및 콘텐츠",
]

# ─── 경기 단계별 주가 변동 범위(%) ────────────────────────────
PHASE_RANGES = {
    1: (-10, 15),
    2: (-5, 30),
    3: (5, 20),
    4: (-10, 15),
    5: (-25, 5),
}


class EconomyCog(commands.Cog):
    """경제 전반(물가 · 지갑 · 주식 · 턴) 관리 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_turn.start()
        self.hourly_stock.start()

    def cog_unload(self):
        self.daily_turn.cancel()
        self.hourly_stock.cancel()

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
                """INSERT INTO stocks (company_name, owner_id, sector_name, current_price, total_shares, economic_phase)
                   VALUES (?, ?, ?, ?, ?, 3)""",
                (기업명, uid, 섹터명, 초기주가, 발행주식수),
            )
            await db.execute(
                """INSERT OR REPLACE INTO stock_holdings (user_id, company_name, quantity) VALUES (?, ?, ?)""",
                (uid, 기업명, 발행주식수),
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
        await interaction.response.send_message(embed=embed)

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
            await db.execute(
                "UPDATE stocks SET current_price = ? WHERE company_name = ?",
                (새로운주가, 기업명),
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

        phase_desc = {
            1: "⬇️ 불황 (-10%~+15%)",
            2: "📉 회복기 (-5%~+30%)",
            3: "📊 보통 (+5%~+20%)",
            4: "📈 경기과열 (-10%~+15%)",
            5: "💥 공황 (-25%~+5%)",
        }
        embed = discord.Embed(
            title="🏢 기업 상황 설정 완료",
            description=f"**{기업명}** → {phase_desc[경기단계1_5]}",
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

        phase_emoji = {1: "⬇️", 2: "📉", 3: "📊", 4: "📈", 5: "💥"}
        lines = []
        for i, r in enumerate(rows, 1):
            pe = phase_emoji.get(r["economic_phase"], "❓")
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
    #  매수
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="매수", description="주식을 매수합니다")
    @app_commands.describe(기업명="매수할 기업", 수량="매수할 주식 수")
    async def buy_stock(
        self, interaction: discord.Interaction, 기업명: str, 수량: int
    ):
        if 수량 <= 0:
            return await interaction.response.send_message(
                "❌ 1주 이상 매수해야 합니다.", ephemeral=True
            )
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT current_price, total_shares FROM stocks WHERE company_name = ?", (기업명,)
            )
            stock = await cur.fetchone()
            if not stock:
                return await interaction.response.send_message(
                    "❌ 해당 기업이 존재하지 않습니다.", ephemeral=True
                )

            price = stock[0]
            total_shares = stock[1]
            total_cost = price * 수량

            cur = await db.execute(
                "SELECT money FROM users WHERE user_id = ?", (uid,)
            )
            wallet = (await cur.fetchone())[0]
            if wallet < total_cost:
                return await interaction.response.send_message(
                    f"❌ 잔액 부족! 필요: {total_cost:,}원 / 보유: {wallet:,}원",
                    ephemeral=True,
                )

            await db.execute(
                "UPDATE users SET money = money - ? WHERE user_id = ?",
                (total_cost, uid),
            )
            await db.execute(
                """INSERT INTO stock_holdings (user_id, company_name, quantity)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, company_name)
                   DO UPDATE SET quantity = quantity + ?""",
                (uid, 기업명, 수량, 수량),
            )
            
            # 주가 상승 로직: 매수 비율(수량/총주식수)에 비례해 상승 (1% 매수 시 0.5% 상승)
            price_increase_ratio = (수량 / total_shares) * 0.5
            new_price = max(1, int(price * (1 + price_increase_ratio)))
            await db.execute(
                "UPDATE stocks SET current_price = ? WHERE company_name = ?", (new_price, 기업명)
            )

            await db.commit()

        embed = discord.Embed(
            title="📈 매수 체결",
            description=(
                f"**{기업명}** {수량:,}주 매수 완료\n"
                f"체결 단가: {price:,}원 · 총액: {total_cost:,}원\n"
                f"*(매수 영향으로 주가가 {new_price:,}원으로 올랐습니다)*"
            ),
            colour=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  매도
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="매도", description="주식을 매도합니다")
    @app_commands.describe(기업명="매도할 기업", 수량="매도할 주식 수")
    async def sell_stock(
        self, interaction: discord.Interaction, 기업명: str, 수량: int
    ):
        if 수량 <= 0:
            return await interaction.response.send_message(
                "❌ 1주 이상 매도해야 합니다.", ephemeral=True
            )
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT quantity FROM stock_holdings WHERE user_id = ? AND company_name = ?",
                (uid, 기업명),
            )
            row = await cur.fetchone()
            if not row or row[0] < 수량:
                owned = row[0] if row else 0
                return await interaction.response.send_message(
                    f"❌ 보유 주식 부족! 보유: {owned:,}주", ephemeral=True
                )

            cur = await db.execute(
                "SELECT current_price, total_shares FROM stocks WHERE company_name = ?", (기업명,)
            )
            stock = await cur.fetchone()
            price = stock[0]
            total_shares = stock[1]
            revenue = price * 수량

            await db.execute(
                "UPDATE users SET money = money + ? WHERE user_id = ?",
                (revenue, uid),
            )
            await db.execute(
                "UPDATE stock_holdings SET quantity = quantity - ? WHERE user_id = ? AND company_name = ?",
                (수량, uid, 기업명),
            )
            # 0주 보유 시 행 삭제
            await db.execute(
                "DELETE FROM stock_holdings WHERE user_id = ? AND company_name = ? AND quantity <= 0",
                (uid, 기업명),
            )
            
            # 주가 하락 로직: 매도 비율(수량/총주식수)에 비례해 하락 (1% 매도 시 0.5% 하락)
            price_decrease_ratio = (수량 / total_shares) * 0.5
            new_price = max(1, int(price * (1 - price_decrease_ratio)))
            await db.execute(
                "UPDATE stocks SET current_price = ? WHERE company_name = ?", (new_price, 기업명)
            )

            await db.commit()

        embed = discord.Embed(
            title="📉 매도 체결",
            description=(
                f"**{기업명}** {수량:,}주 매도 완료\n"
                f"체결 단가: {price:,}원 · 수익: {revenue:,}원\n"
                f"*(매도 영향으로 주가가 {new_price:,}원으로 떨어졌습니다)*"
            ),
            colour=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  매시간 주가 변동 (1시간 주기 자동 실행)
    # ═════════════════════════════════════════════════════════
    @tasks.loop(hours=1)
    async def hourly_stock(self):
        print("[ECONOMY] 매시간 정각 — 주식 시장 변동 시작")
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT company_name, current_price, economic_phase FROM stocks"
            )
            rows = await cur.fetchall()

            for r in rows:
                phase = r["economic_phase"]
                low, high = PHASE_RANGES.get(phase, (0, 0))
                pct = random.uniform(low, high)
                new_price = max(1, int(r["current_price"] * (1 + pct / 100)))
                await db.execute(
                    "UPDATE stocks SET current_price = ? WHERE company_name = ?",
                    (new_price, r["company_name"]),
                )
            await db.commit()

    @hourly_stock.before_loop
    async def before_hourly_stock(self):
        await self.bot.wait_until_ready()

    # ═════════════════════════════════════════════════════════
    #  턴 넘기기 (매일 00시 자동 실행)
    # ═════════════════════════════════════════════════════════
    @tasks.loop(time=time(hour=0, minute=0, tzinfo=timezone(timedelta(hours=9))))
    async def daily_turn(self):
        print("[ECONOMY] 00시 정각 — 연봉 차감 및 스포츠 시즌 마감 시작")
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row

            # ── 선수 연봉(몸값) 차감 로직 ──
            cur = await db.execute("SELECT cumulative_inflation FROM server_settings WHERE id = 1")
            row = await cur.fetchone()
            inf = row["cumulative_inflation"] if row else 1.0

            cur = await db.execute('''
                SELECT st.owner_id, sum(sp.base_transfer_fee) as total_cost
                FROM soccer_players sp
                JOIN sports_teams st ON sp.team_name = st.team_name
                WHERE sp.team_name != '무소속'
                GROUP BY st.owner_id
            ''')
            for owner in await cur.fetchall():
                cost = int(owner["total_cost"] * inf)
                await db.execute("UPDATE users SET money = money - ? WHERE user_id = ?", (cost, owner["owner_id"]))

            cur = await db.execute('''
                SELECT st.owner_id, sum(bp.base_transfer_fee) as total_cost
                FROM baseball_players bp
                JOIN sports_teams st ON bp.team_name = st.team_name
                WHERE bp.team_name != '무소속'
                GROUP BY st.owner_id
            ''')
            for owner in await cur.fetchall():
                cost = int(owner["total_cost"] * inf)
                await db.execute("UPDATE users SET money = money - ? WHERE user_id = ?", (cost, owner["owner_id"]))

            await db.commit()

        # 주가 변동 결과를 경제 관련 채널에 보낼 수 있으면 좋지만,
        # 현재 경제 전용 채널 설정이 없으므로 콘솔에 출력하거나 서버 내 첫 번째 텍스트 채널에 보낼 수 있습니다.
        # 일단은 스포츠 시즌 초기화 이벤트를 발생시킵니다.
        self.bot.dispatch("turn_passed")

    @daily_turn.before_loop
    async def before_daily_turn(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
