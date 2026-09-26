"""
cogs/rp_system.py — 역할극 · 판정 · 아이템 · 인벤토리 시스템
던전월드 기반 2d6 판정, 웹훅 대사, 복리 물가 아이템 상점
"""

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import random

STAT_NAMES = {"힘": "str", "속도": "spd", "지능": "int"}
STAT_BONUS_MAP = {"str": "str_bonus", "spd": "spd_bonus", "int": "int_bonus"}


class RPSystemCog(commands.Cog):
    """역할극 · 판정 · 아이템 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db_path

    # ═════════════════════════════════════════════════════════
    #  아이템 등록 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="아이템등록", description="아이템을 등록합니다 (관리자)")
    @app_commands.describe(이름="아이템 이름", 기본가격="기본 가격", 힘="힘 보너스", 속도="속도 보너스", 지능="지능 보너스")
    @app_commands.checks.has_permissions(administrator=True)
    async def register_item(
        self,
        interaction: discord.Interaction,
        이름: str,
        기본가격: int,
        힘: int = 0,
        속도: int = 0,
        지능: int = 0,
    ):
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                """INSERT INTO items (item_name, base_price, str_bonus, spd_bonus, int_bonus)
                   VALUES (?, ?, ?, ?, ?)""",
                (이름, 기본가격, 힘, 속도, 지능),
            )
            await db.commit()

        embed = discord.Embed(
            title="🛡️ 아이템 등록 완료",
            colour=0x9B59B6,
        )
        embed.add_field(name="이름", value=이름, inline=True)
        embed.add_field(name="기본 가격", value=f"{기본가격:,}원", inline=True)
        embed.add_field(name="보너스", value=f"💪 힘 +{힘} | 🏃 속도 +{속도} | 🧠 지능 +{지능}", inline=False)
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  아이템 삭제 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="아이템삭제", description="아이템을 삭제합니다 (관리자)")
    @app_commands.describe(아이템명="삭제할 아이템 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_item(self, interaction: discord.Interaction, 아이템명: str):
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute("SELECT item_id FROM items WHERE item_name = ?", (아이템명,))
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message("❌ 해당 아이템이 존재하지 않습니다.", ephemeral=True)
            
            item_id = row[0]
            await db.execute("DELETE FROM items WHERE item_id = ?", (item_id,))
            await db.execute("DELETE FROM inventory WHERE item_id = ?", (item_id,))
            await db.commit()

        embed = discord.Embed(
            title="🗑️ 아이템 삭제 완료",
            description=f"**{아이템명}**이(가) 상점과 유저 인벤토리에서 삭제되었습니다.",
            colour=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  아이템 상점 (물가 반영)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="아이템상점", description="현재 상점의 아이템 목록을 확인합니다")
    async def item_shop(self, interaction: discord.Interaction):
        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT cumulative_inflation FROM server_settings WHERE id = 1"
            )
            inflation = (await cur.fetchone())["cumulative_inflation"]

            cur = await db.execute(
                "SELECT item_id, item_name, base_price, str_bonus, spd_bonus, int_bonus FROM items"
            )
            items = await cur.fetchall()

        if not items:
            return await interaction.response.send_message(
                "📭 등록된 아이템이 없습니다.", ephemeral=True
            )

        lines = []
        for item in items:
            sell_price = int(item["base_price"] * inflation)
            bonus_parts = []
            if item["str_bonus"]:
                bonus_parts.append(f"💪+{item['str_bonus']}")
            if item["spd_bonus"]:
                bonus_parts.append(f"🏃+{item['spd_bonus']}")
            if item["int_bonus"]:
                bonus_parts.append(f"🧠+{item['int_bonus']}")
            bonus_str = " ".join(bonus_parts) if bonus_parts else "보너스 없음"
            lines.append(
                f"**[{item['item_id']}] {item['item_name']}** — "
                f"💰 {sell_price:,}원\n"
                f"   {bonus_str}"
            )

        embed = discord.Embed(
            title="🏪 아이템 상점",
            description="\n\n".join(lines),
            colour=0xE67E22,
        )
        embed.set_footer(text=f"현재 물가 배수: ×{inflation:.4f}")
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  아이템 구매
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="아이템구매", description="상점에서 아이템을 구매합니다")
    @app_commands.describe(아이템명="구매할 아이템 이름")
    async def buy_item(self, interaction: discord.Interaction, 아이템명: str):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT cumulative_inflation FROM server_settings WHERE id = 1"
            )
            inflation = (await cur.fetchone())["cumulative_inflation"]

            cur = await db.execute(
                "SELECT item_id, item_name, base_price FROM items WHERE item_name = ?",
                (아이템명,),
            )
            item = await cur.fetchone()
            if not item:
                return await interaction.response.send_message(
                    "❌ 해당 아이템이 존재하지 않습니다.", ephemeral=True
                )

            sell_price = int(item["base_price"] * inflation)

            cur = await db.execute(
                "SELECT money FROM users WHERE user_id = ?", (uid,)
            )
            money = (await cur.fetchone())["money"]
            if money < sell_price:
                return await interaction.response.send_message(
                    f"❌ 잔액 부족! 필요: {sell_price:,}원 / 보유: {money:,}원",
                    ephemeral=True,
                )

            await db.execute(
                "UPDATE users SET money = money - ? WHERE user_id = ?",
                (sell_price, uid),
            )
            await db.execute(
                "INSERT INTO inventory (user_id, item_id, is_equipped) VALUES (?, ?, FALSE)",
                (uid, item["item_id"]),
            )
            await db.commit()

        embed = discord.Embed(
            title="🛒 구매 완료!",
            description=f"**{아이템명}**을(를) **{sell_price:,}원**에 구매했습니다!",
            colour=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  내 인벤토리
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="내인벤토리", description="내 인벤토리와 스탯을 확인합니다")
    async def my_inventory(self, interaction: discord.Interaction):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            # 기본 스탯
            cur = await db.execute(
                "SELECT str, spd, int FROM users WHERE user_id = ?", (uid,)
            )
            user = await cur.fetchone()
            base_str = user["str"]
            base_spd = user["spd"]
            base_int = user["int"]

            # 인벤토리
            cur = await db.execute(
                """SELECT i.item_name, i.str_bonus, i.spd_bonus, i.int_bonus, inv.is_equipped, inv.rowid
                   FROM inventory inv
                   JOIN items i ON inv.item_id = i.item_id
                   WHERE inv.user_id = ?""",
                (uid,),
            )
            inv_items = await cur.fetchall()

        equipped = [it for it in inv_items if it["is_equipped"]]
        unequipped = [it for it in inv_items if not it["is_equipped"]]

        # 장착 보너스 합산
        eq_str = sum(it["str_bonus"] for it in equipped)
        eq_spd = sum(it["spd_bonus"] for it in equipped)
        eq_int = sum(it["int_bonus"] for it in equipped)

        final_str = base_str + eq_str
        final_spd = base_spd + eq_spd
        final_int = base_int + eq_int

        embed = discord.Embed(
            title=f"🎒 {interaction.user.display_name}의 인벤토리",
            colour=0x9B59B6,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # 장착 슬롯
        if equipped:
            eq_lines = [
                f"⚔️ **{it['item_name']}** (💪+{it['str_bonus']} 🏃+{it['spd_bonus']} 🧠+{it['int_bonus']})"
                for it in equipped
            ]
            embed.add_field(
                name=f"🔷 장착 중 ({len(equipped)}/3)",
                value="\n".join(eq_lines),
                inline=False,
            )
        else:
            embed.add_field(name="🔷 장착 중 (0/3)", value="비어 있음", inline=False)

        # 미장착 아이템
        if unequipped:
            ue_lines = [f"• {it['item_name']}" for it in unequipped]
            embed.add_field(
                name="📦 보관 중",
                value="\n".join(ue_lines),
                inline=False,
            )

        # 최종 스탯
        embed.add_field(
            name="📊 최종 스탯",
            value=(
                f"💪 힘: {base_str} (+{eq_str}) = **{final_str}**\n"
                f"🏃 속도: {base_spd} (+{eq_spd}) = **{final_spd}**\n"
                f"🧠 지능: {base_int} (+{eq_int}) = **{final_int}**"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  장착
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="장착", description="인벤토리의 아이템을 장착합니다 (최대 3칸)")
    @app_commands.describe(아이템명="장착할 아이템 이름")
    async def equip(self, interaction: discord.Interaction, 아이템명: str):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            # 현재 장착 수 확인
            cur = await db.execute(
                "SELECT COUNT(*) FROM inventory WHERE user_id = ? AND is_equipped = TRUE",
                (uid,),
            )
            equipped_count = (await cur.fetchone())[0]
            if equipped_count >= 3:
                return await interaction.response.send_message(
                    "❌ 장착 슬롯이 가득 찼습니다! (최대 3칸) 먼저 장착 해제하세요.",
                    ephemeral=True,
                )

            # 미장착 아이템 찾기
            cur = await db.execute(
                """SELECT inv.rowid FROM inventory inv
                   JOIN items i ON inv.item_id = i.item_id
                   WHERE inv.user_id = ? AND i.item_name = ? AND inv.is_equipped = FALSE
                   LIMIT 1""",
                (uid, 아이템명),
            )
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message(
                    "❌ 장착 가능한 해당 아이템이 인벤토리에 없습니다.",
                    ephemeral=True,
                )

            await db.execute(
                "UPDATE inventory SET is_equipped = TRUE WHERE rowid = ?",
                (row[0],),
            )
            await db.commit()

        embed = discord.Embed(
            title="⚔️ 장착 완료",
            description=f"**{아이템명}**을(를) 장착했습니다! ({equipped_count + 1}/3)",
            colour=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  장착 해제
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="장착해제", description="장착 중인 아이템을 해제합니다")
    @app_commands.describe(아이템명="해제할 아이템 이름")
    async def unequip(self, interaction: discord.Interaction, 아이템명: str):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                """SELECT inv.rowid FROM inventory inv
                   JOIN items i ON inv.item_id = i.item_id
                   WHERE inv.user_id = ? AND i.item_name = ? AND inv.is_equipped = TRUE
                   LIMIT 1""",
                (uid, 아이템명),
            )
            row = await cur.fetchone()
            if not row:
                return await interaction.response.send_message(
                    "❌ 해당 아이템이 장착되어 있지 않습니다.",
                    ephemeral=True,
                )

            await db.execute(
                "UPDATE inventory SET is_equipped = FALSE WHERE rowid = ?",
                (row[0],),
            )
            await db.commit()

        embed = discord.Embed(
            title="🔓 장착 해제",
            description=f"**{아이템명}**을(를) 장착 해제했습니다.",
            colour=0xE67E22,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  스탯 조정 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="스탯조정", description="유저의 기본 스탯을 조정합니다 (관리자)")
    @app_commands.describe(유저="대상 유저", 스탯종류="힘/속도/지능", 증감수치="더하거나 뺄 수치")
    @app_commands.checks.has_permissions(administrator=True)
    async def adjust_stat(
        self,
        interaction: discord.Interaction,
        유저: discord.Member,
        스탯종류: str,
        증감수치: int,
    ):
        col = STAT_NAMES.get(스탯종류)
        if not col:
            return await interaction.response.send_message(
                "❌ 스탯 종류는 `힘`, `속도`, `지능` 중 하나여야 합니다.",
                ephemeral=True,
            )

        await self.bot.ensure_user(유저.id)
        async with aiosqlite.connect(self.db) as db:
            # SQLite 예약어 str/int와 충돌 방지를 위해 따옴표 처리
            await db.execute(
                f'UPDATE users SET "{col}" = "{col}" + ? WHERE user_id = ?',
                (증감수치, 유저.id),
            )
            cur = await db.execute(
                f'SELECT "{col}" FROM users WHERE user_id = ?', (유저.id,)
            )
            new_val = (await cur.fetchone())[0]
            await db.commit()

        sign = "+" if 증감수치 >= 0 else ""
        embed = discord.Embed(
            title="📊 스탯 조정 완료",
            description=(
                f"{유저.mention}의 **{스탯종류}** 스탯: "
                f"{sign}{증감수치} → 현재 **{new_val}**"
            ),
            colour=0x3498DB,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  판정 (던전월드 2d6)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="판정", description="2d6 + 스탯 판정을 수행합니다")
    @app_commands.describe(스탯종류="힘/속도/지능", 행동목적="무엇을 하려는지 서술")
    async def roll_check(
        self, interaction: discord.Interaction, 스탯종류: str, 행동목적: str
    ):
        col = STAT_NAMES.get(스탯종류)
        if not col:
            return await interaction.response.send_message(
                "❌ 스탯 종류는 `힘`, `속도`, `지능` 중 하나여야 합니다.",
                ephemeral=True,
            )

        uid = interaction.user.id
        await self.bot.ensure_user(uid)
        bonus_col = STAT_BONUS_MAP[col]

        async with aiosqlite.connect(self.db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f'SELECT "{col}" FROM users WHERE user_id = ?', (uid,)
            )
            base_stat = (await cur.fetchone())[col]

            cur = await db.execute(
                f"""SELECT COALESCE(SUM(i.{bonus_col}), 0) AS bonus
                    FROM inventory inv
                    JOIN items i ON inv.item_id = i.item_id
                    WHERE inv.user_id = ? AND inv.is_equipped = TRUE""",
                (uid,),
            )
            equip_bonus = (await cur.fetchone())["bonus"]

        final_stat = base_stat + equip_bonus
        d1 = random.randint(1, 6)
        d2 = random.randint(1, 6)
        dice_sum = d1 + d2
        total = dice_sum + final_stat

        if total >= 10:
            result_title = "✨ 완전 성공!"
            result_colour = 0x2ECC71
            result_desc = f"**{행동목적}** — 훌륭하게 성공했습니다! 의도한 대로 완벽히 이루어집니다."
        elif total >= 7:
            result_title = "⚡ 부분 성공"
            result_colour = 0xF1C40F
            result_desc = f"**{행동목적}** — 어느 정도 성공했지만, 대가나 곤란한 선택이 따릅니다."
        else:
            result_title = "💀 실패..."
            result_colour = 0xE74C3C
            result_desc = f"**{행동목적}** — 실패했습니다. 상황이 더 나빠질 수 있습니다."

        embed = discord.Embed(
            title=result_title,
            description=result_desc,
            colour=result_colour,
        )
        embed.add_field(
            name="🎲 주사위",
            value=f"[ **{d1}** ] + [ **{d2}** ] = {dice_sum}",
            inline=True,
        )
        embed.add_field(
            name=f"📊 {스탯종류} 스탯",
            value=f"기본 {base_stat} + 장비 {equip_bonus} = **{final_stat}**",
            inline=True,
        )
        embed.add_field(
            name="📐 최종 합계",
            value=f"**{total}** (10↑ 성공 / 7~9 부분 / 6↓ 실패)",
            inline=False,
        )
        embed.set_footer(text=f"{interaction.user.display_name}의 판정")
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  RP 프로필 등록
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="RP프로필등록", description="RP 캐릭터 프로필을 등록합니다")
    @app_commands.describe(이름="RP 캐릭터 이름", 프로필이미지="프로필 이미지 URL")
    async def register_rp_profile(
        self, interaction: discord.Interaction, 이름: str, 프로필이미지: str
    ):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE users SET rp_name = ?, profile_url = ? WHERE user_id = ?",
                (이름, 프로필이미지, uid),
            )
            await db.commit()

        embed = discord.Embed(
            title="🎭 RP 프로필 등록 완료",
            colour=0x9B59B6,
        )
        embed.set_thumbnail(url=프로필이미지)
        embed.add_field(name="캐릭터 이름", value=이름, inline=True)
        embed.set_footer(text=f"Discord: {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  캐릭터 삭제 (관리자)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="캐릭터삭제", description="유저의 RP 캐릭터를 삭제합니다 (관리자)")
    @app_commands.describe(유저="캐릭터를 삭제할 유저")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_character(self, interaction: discord.Interaction, 유저: discord.Member):
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "UPDATE users SET rp_name = NULL, profile_url = NULL WHERE user_id = ?", (유저.id,)
            )
            await db.commit()

        embed = discord.Embed(
            title="🗑️ 캐릭터 삭제 완료",
            description=f"{유저.mention}님의 RP 캐릭터 프로필이 초기화되었습니다.",
            colour=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed)

    # ═════════════════════════════════════════════════════════
    #  대사 (Webhook)
    # ═════════════════════════════════════════════════════════
    @app_commands.command(name="대사", description="RP 캐릭터로 대사를 보냅니다 (웹훅)")
    @app_commands.describe(할말="캐릭터가 할 대사")
    async def rp_say(self, interaction: discord.Interaction, 할말: str):
        uid = interaction.user.id
        await self.bot.ensure_user(uid)

        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(
                "SELECT rp_name, profile_url FROM users WHERE user_id = ?", (uid,)
            )
            row = await cur.fetchone()

        if not row or not row[0]:
            return await interaction.response.send_message(
                "❌ 먼저 `/RP프로필등록`으로 캐릭터를 등록하세요.", ephemeral=True
            )

        rp_name, profile_url = row

        # 채널 웹훅 가져오거나 생성
        channel = interaction.channel
        webhooks = await channel.webhooks()
        webhook = discord.utils.get(webhooks, name="VNBot_RP")
        if webhook is None:
            webhook = await channel.create_webhook(name="VNBot_RP")

        await interaction.response.send_message("💬", ephemeral=True, delete_after=1)

        await webhook.send(
            content=할말,
            username=rp_name,
            avatar_url=profile_url if profile_url else None,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RPSystemCog(bot))
