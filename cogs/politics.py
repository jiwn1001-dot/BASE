"""
cogs/politics.py — 가상 국가 정치 시스템
정당 창당 · 당대표 독식 투표 · 법안 발의/투표/종료
"""

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite


class BillView(discord.ui.View):
    """법안 찬반 투표 버튼 UI"""

    def __init__(self, cog: "PoliticsCog", bill_content: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.bill_content = bill_content
        # {party_name: {"choice": "찬성"/"반대", "house": int, "senate": int}}
        self.party_votes: dict[str, dict] = {}
        # 일반 유저 여론 기록
        self.public_opinions: list[str] = []
        self.message_id: int | None = None

    @discord.ui.button(label="찬성", style=discord.ButtonStyle.green, emoji="✅")
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._handle_vote(interaction, "찬성")

    @discord.ui.button(label="반대", style=discord.ButtonStyle.red, emoji="❌")
    async def reject_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self._handle_vote(interaction, "반대")

    async def _handle_vote(self, interaction: discord.Interaction, choice: str):
        uid = interaction.user.id

        async with aiosqlite.connect(self.cog.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT party_name, house_seats, senate_seats FROM parties WHERE rep_user_id = ?",
                (uid,),
            )
            party = await cur.fetchone()

        if party:
            # ── 당대표: 소속 정당 의석 100% 몰표 ──────────
            self.party_votes[party["party_name"]] = {
                "choice": choice,
                "house": party["house_seats"],
                "senate": party["senate_seats"],
            }
            embed = discord.Embed(
                title="🏛️ 당대표 투표 등록",
                description=(
                    f"**{party['party_name']}** 당대표 {interaction.user.mention}이(가) "
                    f"**{choice}**을(를) 선택했습니다.\n"
                    f"하원 {party['house_seats']}석 · 상원 {party['senate_seats']}석이 "
                    f"일괄 반영됩니다."
                ),
                colour=0x3498DB,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # ── 일반 유저: 여론 텍스트만 표시 ─────────────
            opinion = f"{interaction.user.display_name}: {choice}"
            self.public_opinions.append(opinion)
            embed = discord.Embed(
                title="🗣️ 여론 등록",
                description=f"당신의 의견(**{choice}**)이 여론으로 기록되었습니다.\n*(일반 유저의 투표는 의석 합산에 포함되지 않습니다.)*",
                colour=0x95A5A6,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


class PoliticsCog(commands.Cog):
    """정당 · 입법 · 투표 관리 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 활성 법안 저장: {message_id: BillView}
        self.active_bills: dict[int, BillView] = {}

    @property
    def db(self):
        return self.bot.db_path

    # ═════════════════════════════════════════════════════════
    #  정당 창당 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="정당창당", description="정당을 창설합니다 (관리자)")
    @app_commands.describe(정당명="정당 이름", 하원의석="하원 의석 수", 상원의석="상원 의석 수")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_party(
        self,
        interaction: discord.Interaction,
        정당명: str,
        하원의석: int,
        상원의석: int,
    ):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT party_name FROM parties WHERE party_name = ?", (정당명,)
            )
            if await cur.fetchone():
                return await interaction.response.send_message(
                    "❌ 이미 존재하는 정당입니다.", ephemeral=True
                )
            await db.execute(
                """INSERT INTO parties (party_name, rep_user_id, house_seats, senate_seats, approval_rate)
                   VALUES (?, NULL, ?, ?, 50.0)""",
                (정당명, 하원의석, 상원의석),
            )
            await db.commit()

        embed = discord.Embed(
            title="🏛️ 정당 창당",
            description=f"**{정당명}**이(가) 창당되었습니다!",
            colour=0x3498DB,
        )
        embed.add_field(name="🏠 하원 의석", value=f"{하원의석}석", inline=True)
        embed.add_field(name="🏢 상원 의석", value=f"{상원의석}석", inline=True)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  당대표 지정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="당대표지정", description="정당의 당대표를 지정합니다 (관리자)")
    @app_commands.describe(정당명="대상 정당", 유저="당대표로 지정할 유저")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_party_leader(
        self,
        interaction: discord.Interaction,
        정당명: str,
        유저: discord.Member,
    ):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT party_name FROM parties WHERE party_name = ?", (정당명,)
            )
            if not await cur.fetchone():
                return await interaction.response.send_message(
                    "❌ 해당 정당이 존재하지 않습니다.", ephemeral=True
                )
            await db.execute(
                "UPDATE parties SET rep_user_id = ? WHERE party_name = ?",
                (유저.id, 정당명),
            )
            await db.commit()

        embed = discord.Embed(
            title="👑 당대표 지정 완료",
            description=f"**{정당명}**의 당대표가 {유저.mention}(으)로 지정되었습니다.",
            colour=0xF1C40F,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  정당 삭제 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="정당삭제", description="정당을 삭제합니다 (관리자)")
    @app_commands.describe(정당명="삭제할 정당 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_party(self, interaction: discord.Interaction, 정당명: str):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT party_name FROM parties WHERE party_name = ?", (정당명,))
            if not await cur.fetchone():
                return await interaction.response.send_message("❌ 해당 정당이 존재하지 않습니다.", ephemeral=True)
            
            await db.execute("DELETE FROM parties WHERE party_name = ?", (정당명,))
            await db.commit()

        embed = discord.Embed(
            title="🗑️ 정당 삭제 완료",
            description=f"**{정당명}**이(가) 해산되었습니다.",
            colour=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  의석 수정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="의석수정", description="정당의 의석 수를 수정합니다 (관리자)")
    @app_commands.describe(정당명="대상 정당", 하원의석="새로운 하원 의석 수", 상원의석="새로운 상원 의석 수")
    @app_commands.checks.has_permissions(administrator=True)
    async def modify_seats(self, interaction: discord.Interaction, 정당명: str, 하원의석: int, 상원의석: int):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT party_name FROM parties WHERE party_name = ?", (정당명,))
            if not await cur.fetchone():
                return await interaction.response.send_message("❌ 해당 정당이 존재하지 않습니다.", ephemeral=True)
            
            await db.execute(
                "UPDATE parties SET house_seats = ?, senate_seats = ? WHERE party_name = ?",
                (하원의석, 상원의석, 정당명)
            )
            await db.commit()

        embed = discord.Embed(
            title="🔄 의석 수정 완료",
            description=f"**{정당명}**의 의석이 수정되었습니다.",
            colour=0x3498DB,
        )
        embed.add_field(name="🏠 하원 의석", value=f"{하원의석}석", inline=True)
        embed.add_field(name="🏢 상원 의석", value=f"{상원의석}석", inline=True)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  정당 이름 수정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="정당이름수정", description="정당의 이름을 수정합니다 (관리자)")
    @app_commands.describe(기존이름="현재 정당 이름", 새이름="새로운 정당 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def modify_party_name(self, interaction: discord.Interaction, 기존이름: str, 새이름: str):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT party_name FROM parties WHERE party_name = ?", (기존이름,))
            if not await cur.fetchone():
                return await interaction.response.send_message("❌ 해당 정당이 존재하지 않습니다.", ephemeral=True)
            
            cur = await db.execute("SELECT party_name FROM parties WHERE party_name = ?", (새이름,))
            if await cur.fetchone():
                return await interaction.response.send_message("❌ 이미 존재하는 정당 이름입니다.", ephemeral=True)
            
            await db.execute("UPDATE parties SET party_name = ? WHERE party_name = ?", (새이름, 기존이름))
            await db.commit()

        embed = discord.Embed(
            title="🏷️ 정당 이름 변경",
            description=f"**{기존이름}**이(가) **{새이름}**(으)로 당명을 변경했습니다.",
            colour=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  법안 발의
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="법안발의", description="법안을 발의하고 투표를 시작합니다")
    @app_commands.describe(법안내용="법안의 내용")
    async def propose_bill(self, interaction: discord.Interaction, 법안내용: str):
        view = BillView(self, 법안내용)

        embed = discord.Embed(
            title="📜 법안 발의",
            description=f"**{법안내용}**",
            colour=0x3498DB,
        )
        embed.add_field(
            name="📋 투표 안내",
            value=(
                "• **당대표**: 찬성/반대 버튼을 누르면 소속 정당의 "
                "하원·상원 의석이 일괄 반영됩니다.\n"
                "• **일반 유저**: 여론으로만 기록됩니다."
            ),
            inline=False,
        )
        embed.set_footer(text=f"발의자: {interaction.user.display_name} · /투표종료 로 결과 확인")

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        view.message_id = msg.id
        self.active_bills[msg.id] = view

    # ═════════════════════════════════════════════════════════
    #  투표 종료
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="투표종료", description="가장 최근 법안의 투표를 종료합니다")
    @app_commands.checks.has_permissions(administrator=True)
    async def close_vote(self, interaction: discord.Interaction):
        if not self.active_bills:
            return await interaction.response.send_message(
                "❌ 활성화된 법안이 없습니다.", ephemeral=True
            )

        # 가장 최근 법안
        msg_id = max(self.active_bills.keys())
        bill = self.active_bills.pop(msg_id)
        bill.stop()

        # 양원 합산
        house_approve = 0
        house_reject = 0
        senate_approve = 0
        senate_reject = 0

        vote_details = []
        for party_name, data in bill.party_votes.items():
            if data["choice"] == "찬성":
                house_approve += data["house"]
                senate_approve += data["senate"]
            else:
                house_reject += data["house"]
                senate_reject += data["senate"]
            vote_details.append(
                f"🏛️ **{party_name}**: {data['choice']} "
                f"(하원 {data['house']}석 · 상원 {data['senate']}석)"
            )

        total_house = house_approve + house_reject
        total_senate = senate_approve + senate_reject

        house_pass = house_approve > total_house / 2 if total_house > 0 else False
        senate_pass = senate_approve > total_senate / 2 if total_senate > 0 else False
        bill_passed = house_pass and senate_pass

        result_text = "✅ **가결**" if bill_passed else "❌ **부결**"
        result_colour = 0x2ECC71 if bill_passed else 0xE74C3C

        embed = discord.Embed(
            title=f"📜 투표 결과 — {result_text}",
            description=f"**{bill.bill_content}**",
            colour=result_colour,
        )
        embed.add_field(
            name="🏠 하원",
            value=f"찬성 {house_approve}석 / 반대 {house_reject}석\n"
            f"{'✅ 통과' if house_pass else '❌ 미통과'}",
            inline=True,
        )
        embed.add_field(
            name="🏢 상원",
            value=f"찬성 {senate_approve}석 / 반대 {senate_reject}석\n"
            f"{'✅ 통과' if senate_pass else '❌ 미통과'}",
            inline=True,
        )
        if vote_details:
            embed.add_field(
                name="🗳️ 정당별 투표 내역",
                value="\n".join(vote_details),
                inline=False,
            )
        if bill.public_opinions:
            embed.add_field(
                name="🗣️ 시민 여론",
                value="\n".join(bill.public_opinions[:15]),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(PoliticsCog(bot))
