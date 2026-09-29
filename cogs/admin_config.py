"""
cogs/admin_config.py — 관리자용 게임 수치 조절 패널
/설정보기 · /설정변경 · /설정초기화 · /관리자도움말
"""

import discord
from discord.ext import commands
from discord import app_commands

from . import _config as cfg


class AdminConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await cfg.load(self.bot.db_path)

    async def _key_autocomplete(self, interaction: discord.Interaction, current: str):
        out = []
        for k, (_, lo, hi, desc) in cfg.DEFAULTS.items():
            if current in k or current in desc:
                out.append(app_commands.Choice(name=f"{k} = {cfg.fmt(cfg.get(k))} ({desc})"[:100], value=k))
        return out[:25]

    # ─────────────────────────────────────────────────────────
    @app_commands.command(name="설정보기", description="게임 수치 설정 현황을 봅니다 (관리자)")
    @app_commands.describe(분류="주식 / 경기 / 축구 / 야구 (비우면 전체)")
    @app_commands.choices(분류=[app_commands.Choice(name=c, value=c) for c in cfg.CATEGORIES])
    @app_commands.checks.has_permissions(administrator=True)
    async def show_config(self, interaction: discord.Interaction, 분류: app_commands.Choice[str] = None):
        cats = [분류.value] if 분류 else list(cfg.CATEGORIES)
        embed = discord.Embed(title="⚙️ 게임 수치 설정", colour=0x95A5A6)
        for c in cats:
            lines = []
            for k, (default, lo, hi, desc) in cfg.DEFAULTS.items():
                if not k.startswith(c + "_"):
                    continue
                v = cfg.get(k)
                mark = "" if v == default else f" ✏️(기본 {cfg.fmt(default)})"
                lines.append(f"`{k}` **{cfg.fmt(v)}**{mark}\n└ {desc}")
            # 필드당 1024자 제한 → 나눠서 추가
            chunk, part = [], 1
            for line in lines:
                if len("\n".join(chunk + [line])) > 1000:
                    embed.add_field(name=f"{cfg.CATEGORIES[c]} {c} ({part})", value="\n".join(chunk), inline=False)
                    chunk, part = [], part + 1
                chunk.append(line)
            if chunk:
                embed.add_field(name=f"{cfg.CATEGORIES[c]} {c}" + (f" ({part})" if part > 1 else ""),
                                value="\n".join(chunk), inline=False)
        embed.set_footer(text="/설정변경 항목 값 으로 바로 적용 · ✏️ = 기본값에서 바뀐 항목")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="설정변경", description="게임 수치를 즉시 변경합니다 (관리자)")
    @app_commands.describe(항목="바꿀 설정 (입력하면 자동완성)", 값="새 값")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_config(self, interaction: discord.Interaction, 항목: str, 값: float):
        if 항목 not in cfg.DEFAULTS:
            return await interaction.response.send_message(
                "❌ 없는 항목입니다. 자동완성 목록에서 골라주세요.", ephemeral=True)
        old = cfg.get(항목)
        new = await cfg.set_value(self.bot.db_path, 항목, 값)
        _, lo, hi, desc = cfg.DEFAULTS[항목]
        note = f"\n⚠️ 허용 범위({cfg.fmt(lo)}~{cfg.fmt(hi)})로 조정됨" if new != 값 else ""
        if 항목.startswith("경기_") and 항목 in ("경기_중계시간초", "경기_최소휴식초", "경기_목표휴식초"):
            note += "\n📅 일정 관련 설정은 `/시즌시작` 을 하거나 다음 00시 시즌부터 적용됩니다."
        self.bot.dispatch("config_changed", 항목)
        await interaction.response.send_message(
            f"✅ `{항목}` {cfg.fmt(old)} → **{cfg.fmt(new)}**\n└ {desc}{note}")

    set_config.autocomplete("항목")(_key_autocomplete)

    @app_commands.command(name="설정초기화", description="설정을 기본값으로 되돌립니다 (관리자)")
    @app_commands.describe(항목="되돌릴 항목 (비우면 전체 초기화)")
    @app_commands.checks.has_permissions(administrator=True)
    async def reset_config(self, interaction: discord.Interaction, 항목: str = None):
        if 항목 and 항목 not in cfg.DEFAULTS:
            return await interaction.response.send_message("❌ 없는 항목입니다.", ephemeral=True)
        await cfg.reset(self.bot.db_path, 항목)
        self.bot.dispatch("config_changed", 항목)
        await interaction.response.send_message(
            f"♻️ {'`' + 항목 + '`' if 항목 else '모든 설정'}을(를) 기본값으로 되돌렸습니다.")

    reset_config.autocomplete("항목")(_key_autocomplete)

    # ─────────────────────────────────────────────────────────
    @app_commands.command(name="관리자도움말", description="관리자 명령어 모음을 봅니다")
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🛠️ 관리자 명령어", colour=0x2C3E50)
        embed.add_field(name="⚙️ 수치 조절", value=(
            "`/설정보기` 전체 수치 확인\n"
            "`/설정변경` 주가 변동성·뉴스 빈도·상하한가·수수료·경기 길이·득점/홈런 배율 등\n"
            "`/설정초기화` 기본값 복구"
        ), inline=False)
        embed.add_field(name="📈 주식", value=(
            "`/주식채널설정` 속보·시황 채널\n"
            "`/기업배정` `/기업삭제` `/주가강제조정` `/적정가설정`\n"
            "`/기업상황설정` `/섹터상황설정` 경기 단계(1~5)\n"
            "`/뉴스발생` 개별 호재/악재 · `/시장이벤트` 시장·섹터 전체 충격\n"
            "`/유상증자` · `/주식지급` (음수면 회수) · `/대주주지분설정` 유통 물량 조절"
        ), inline=False)
        embed.add_field(name="⚽⚾ 스포츠", value=(
            "`/축구채널설정` `/야구채널설정` · `/시즌시작` 일정 재편성\n"
            "`/구단창설권한` `/구단이름수정` `/구단삭제` · `/전적수정` `/순위초기화`\n"
            "`/선수등록` `/선수삭제` `/선수능력치수정` `/선수몸값수정` `/선수강제이적`\n"
            "`/기본선수세팅` `/선수초기화`"
        ), inline=False)
        embed.add_field(name="💰 경제", value="`/물가상승` `/잔액설정` `/연봉설정`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminConfigCog(bot))
