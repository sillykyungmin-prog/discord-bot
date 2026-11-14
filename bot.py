"""
Discord 관리 봇 - 단일 파일 버전
Python 3.10+
설치: py -m pip install -U discord.py
실행: Windows PowerShell에서 $env:DISCORD_TOKEN="봇_토큰"; py bot.py

필수 Developer Portal 설정:
- Bot > Privileged Gateway Intents:
  SERVER MEMBERS INTENT
  MESSAGE CONTENT INTENT
  PRESENCE INTENT (유저정보의 온라인 상태/활동 조회용)
- 봇 초대 권한:
  View Channels, Send Messages, Embed Links, Read Message History,
  Manage Messages, Moderate Members, Kick Members, Ban Members, View Audit Log
- 봇 역할을 관리 대상 유저의 역할보다 위에 배치해야 합니다.

명령어는 서버별로 시작 시 동기화되므로 이미 봇이 있는 서버에도 반영됩니다.
코드를 추가/수정한 뒤에는 파일을 저장하고 봇을 재시작해야 합니다.

참고:
Discord API는 온보딩 화면에서 사용자가 고른 임의의 "공지 채널" 값을
별도 필드로 항상 제공하지 않습니다. 이 코드는 커뮤니티 서버의
공개 업데이트 채널(public_updates_channel)을 먼저 찾고, 없으면
시스템 채널(system_channel)을 사용합니다.
"""

from __future__ import annotations

import os
import re
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

# -------------------- 설정 --------------------

TOKEN = os.getenv("DISCORD_TOKEN")
TIMEOUT_MINUTES = 10
SPAM_LIMIT = 5
SPAM_WINDOW_SECONDS = 180
MAX_TIMEOUT_MINUTES = 28 * 24
MAX_DELETE_COUNT = 1000


# 들낙 감지 설정 및 영구 저장소
# Railway에서는 Volume을 /data에 연결하고 BOT_DATABASE_PATH=/data/bot_data.sqlite3 설정 권장
DATABASE_PATH = os.getenv("BOT_DATABASE_PATH", "bot_data.sqlite3")
LEAVE_WINDOW = timedelta(hours=24)


def init_database() -> None:
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leave_guard_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leave_guard_joins (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at REAL NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sanctions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('warning', 'caution')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                actor_id INTEGER,
                reason TEXT NOT NULL,
                converted_at REAL,
                expired_at REAL,
                removed_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS moderation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor_id INTEGER,
                created_at REAL NOT NULL,
                automatic INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sanctions_user ON sanctions(guild_id, user_id, active, kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON moderation_history(guild_id, user_id, created_at)")
        conn.commit()


def leave_guard_enabled(guild_id: int) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT enabled FROM leave_guard_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return bool(row and row[0])


def set_leave_guard(guild_id: int, enabled: bool) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT INTO leave_guard_settings (guild_id, enabled) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled",
            (guild_id, int(enabled)),
        )
        if not enabled:
            conn.execute("DELETE FROM leave_guard_joins WHERE guild_id = ?", (guild_id,))
        conn.commit()


def record_member_join(guild_id: int, user_id: int, joined_at: float) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT INTO leave_guard_joins (guild_id, user_id, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET joined_at=excluded.joined_at",
            (guild_id, user_id, joined_at),
        )
        conn.commit()


def get_member_join(guild_id: int, user_id: int) -> float | None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT joined_at FROM leave_guard_joins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return float(row[0]) if row else None


def clear_member_join(guild_id: int, user_id: int) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "DELETE FROM leave_guard_joins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        conn.commit()


# -------------------- 제재 기록 / 경고·주의 시스템 --------------------

WARNING_LIMIT = 5
CAUTION_LIMIT = 2
CAUTION_EXPIRY_SECONDS = 7 * 24 * 60 * 60


def db_counts(guild_id: int, user_id: int) -> tuple[int, int]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM sanctions WHERE guild_id=? AND user_id=? AND active=1 GROUP BY kind",
            (guild_id, user_id),
        ).fetchall()
    counts = {kind: count for kind, count in rows}
    return counts.get("warning", 0), counts.get("caution", 0)


def add_history(guild_id: int, user_id: int, action: str, reason: str, actor_id: int | None, automatic: bool = False) -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT INTO moderation_history (guild_id,user_id,action,reason,actor_id,created_at,automatic) VALUES (?,?,?,?,?,?,?)",
            (guild_id, user_id, action, reason[:1000], actor_id, datetime.now(timezone.utc).timestamp(), int(automatic)),
        )
        conn.commit()


def add_sanction(guild_id: int, user_id: int, kind: str, actor_id: int | None, reason: str) -> None:
    now = datetime.now(timezone.utc).timestamp()
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT INTO sanctions (guild_id,user_id,kind,active,created_at,actor_id,reason) VALUES (?,?,?,1,?,?,?)",
            (guild_id, user_id, kind, now, actor_id, reason[:1000]),
        )
        conn.commit()
    add_history(guild_id, user_id, "경고 부여" if kind == "warning" else "주의 부여", reason, actor_id)


def remove_sanction(guild_id: int, user_id: int, kind: str, actor_id: int | None, reason: str) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM sanctions WHERE guild_id=? AND user_id=? AND kind=? AND active=1 ORDER BY created_at ASC, id ASC LIMIT 1",
            (guild_id, user_id, kind),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE sanctions SET active=0, removed_at=? WHERE id=?", (now, row[0]))
        conn.commit()
    add_history(guild_id, user_id, "경고 차감" if kind == "warning" else "주의 차감", reason, actor_id)
    return True


async def get_actor_label(guild: discord.Guild, actor_id: int | None) -> str:
    if actor_id is None:
        return "자동 시스템"
    member = guild.get_member(actor_id)
    if member:
        return f"{member} ({member.id})"
    try:
        user = await bot.fetch_user(actor_id)
        return f"{user} ({user.id})"
    except discord.HTTPException:
        return f"알 수 없는 관리자 ({actor_id})"


async def send_action_dm(
    user: discord.abc.User, guild: discord.Guild, action: str, reason: str,
    actor_id: int | None, warning_count: int | None = None, caution_count: int | None = None,
) -> bool:
    if warning_count is None or caution_count is None:
        warning_count, caution_count = db_counts(guild.id, user.id)
    actor_label = await get_actor_label(guild, actor_id)
    embed = make_embed("서버 관리 안내", f"**{guild.name}** 서버에서 관리 조치가 적용되었습니다.", discord.Color.orange())
    embed.add_field(name="조치 내용", value=action[:1024], inline=False)
    embed.add_field(name="조치 사유", value=(reason or "사유가 입력되지 않았습니다.")[:1024], inline=False)
    embed.add_field(name="조치한 관리자", value=actor_label[:1024], inline=False)
    embed.add_field(name="현재 경고 / 주의", value=f"경고 **{warning_count}/{WARNING_LIMIT}개** · 주의 **{caution_count}/{CAUTION_LIMIT}개**", inline=False)
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.info("DM 전송 실패 또는 차단: guild=%s user=%s action=%s", guild.id, user.id, action)
        return False


async def apply_warning_threshold(guild: discord.Guild, user: discord.abc.User, actor_id: int | None, reason: str) -> bool:
    warnings, cautions = db_counts(guild.id, user.id)
    if warnings < WARNING_LIMIT:
        return False
    me = guild.me
    if me is None or not me.guild_permissions.ban_members:
        log.warning("경고 누적 자동 차단을 수행할 권한이 없습니다: guild=%s user=%s", guild.id, user.id)
        return False
    if user.id == guild.owner_id or (isinstance(user, discord.Member) and user.top_role >= me.top_role):
        log.warning("경고 누적 자동 차단 대상의 역할을 처리할 수 없습니다: guild=%s user=%s", guild.id, user.id)
        return False
    try:
        await guild.ban(user, reason="경고 5개 누적에 따른 자동 차단", delete_message_seconds=0)
        add_history(guild.id, user.id, "자동 차단", "경고 5개 누적", actor_id, automatic=True)
        await send_action_dm(user, guild, "경고 5개 누적으로 서버 자동 차단", "경고 5개 누적", actor_id)
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.exception("경고 누적 자동 차단 실패: guild=%s user=%s", guild.id, user.id)
        return False


async def grant_caution(guild: discord.Guild, user: discord.Member, actor_id: int, reason: str) -> tuple[str, int, int]:
    # 명령 실행 시점에 이미 7일이 지난 주의는 먼저 만료시켜 전환 대상에서 제외
    await expire_cautions_once()
    add_sanction(guild.id, user.id, "caution", actor_id, reason)
    warnings, cautions = db_counts(guild.id, user.id)
    action = "주의 1개 부여"
    if cautions >= CAUTION_LIMIT:
        now = datetime.now(timezone.utc).timestamp()
        with sqlite3.connect(DATABASE_PATH) as conn:
            rows = conn.execute(
                "SELECT id FROM sanctions WHERE guild_id=? AND user_id=? AND kind='caution' AND active=1 ORDER BY created_at ASC, id ASC LIMIT 2",
                (guild.id, user.id),
            ).fetchall()
            if len(rows) == 2:
                conn.executemany(
                    "UPDATE sanctions SET active=0, converted_at=? WHERE id=?",
                    [(now, row[0]) for row in rows],
                )
                conn.execute(
                    "INSERT INTO sanctions (guild_id,user_id,kind,active,created_at,actor_id,reason) VALUES (?,?, 'warning',1,?,?,?)",
                    (guild.id, user.id, now, actor_id, "주의 2개 자동 전환"),
                )
                conn.commit()
                add_history(guild.id, user.id, "주의 2개 → 경고 1개 전환", "주의 2개 누적", actor_id, automatic=True)
                action = "주의 1개 부여 및 주의 2개 → 경고 1개 자동 전환"
    warnings, cautions = db_counts(guild.id, user.id)
    return action, warnings, cautions


async def expire_cautions_once() -> None:
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - CAUTION_EXPIRY_SECONDS
    with sqlite3.connect(DATABASE_PATH) as conn:
        rows = conn.execute(
            "SELECT id,guild_id,user_id,actor_id,reason FROM sanctions WHERE kind='caution' AND active=1 AND created_at<=? ORDER BY created_at ASC",
            (cutoff,),
        ).fetchall()
        for row in rows:
            sanction_id, guild_id, user_id, actor_id, old_reason = row
            conn.execute("UPDATE sanctions SET active=0, expired_at=? WHERE id=? AND active=1", (now, sanction_id))
            conn.execute(
                "INSERT INTO moderation_history (guild_id,user_id,action,reason,actor_id,created_at,automatic) VALUES (?,?,?,?,?,?,1)",
                (guild_id, user_id, "주의 자동 만료", "주의 부여 후 7일 경과", None, now),
            )
        conn.commit()
    for sanction_id, guild_id, user_id, actor_id, old_reason in rows:
        guild = bot.get_guild(guild_id)
        if guild:
            try:
                user = await bot.fetch_user(user_id)
                await send_action_dm(user, guild, "주의 1개 자동 차감", "주의 부여 후 7일이 지나 자동 만료되었습니다.", None)
            except discord.HTTPException:
                pass


@tasks.loop(minutes=1)
async def caution_expiry_loop():
    try:
        await expire_cautions_once()
    except Exception:
        log.exception("주의 자동 만료 확인 중 오류")


@caution_expiry_loop.before_loop
async def before_caution_expiry_loop():
    await bot.wait_until_ready()


init_database()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("server-management-bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.presences = True

# user별 연속 메시지 감지 상태
# key: (guild_id, user_id)
spam_states: dict[tuple[int, int], dict] = {}


class ManagementBot(commands.Bot):
    async def setup_hook(self) -> None:
        # 전역 명령어도 동기화하고, 각 서버에 길드 명령어로 복사해 즉시 반영
        try:
            await self.tree.sync()
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("명령어 동기화 완료: %s (%s개)", guild.name, len(synced))
        except Exception:
            log.exception("명령어 동기화 중 오류 발생")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("새 서버 명령어 동기화: %s (%s개)", guild.name, len(synced))
        except Exception:
            log.exception("새 서버 동기화 실패: %s", guild.id)


bot = ManagementBot(command_prefix="!", intents=intents)


leave_guard_group = app_commands.Group(
    name="들낙",
    description="24시간 이내 자진 퇴장 감지 기능을 설정합니다.",
)


@leave_guard_group.command(name="켜기", description="이 서버의 들낙 감지를 켭니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def leave_guard_on(interaction: discord.Interaction):
    set_leave_guard(interaction.guild_id, True)
    await interaction.response.send_message(
        "이 서버의 들낙 감지를 켰습니다. 이제 새로 입장한 멤버를 24시간 동안 확인합니다.",
        ephemeral=True,
    )


@leave_guard_group.command(name="끄기", description="이 서버의 들낙 감지를 끕니다.")
@app_commands.checks.has_permissions(manage_guild=True)
async def leave_guard_off(interaction: discord.Interaction):
    set_leave_guard(interaction.guild_id, False)
    await interaction.response.send_message(
        "이 서버의 들낙 감지를 껐습니다. 저장된 입장 기록도 초기화했습니다.",
        ephemeral=True,
    )


bot.tree.add_command(leave_guard_group)


# -------------------- /경고 /주의 --------------------

@bot.tree.command(name="경고", description="유저의 경고를 부여하거나 차감합니다.")
@app_commands.choices(처리=[
    app_commands.Choice(name="부여", value="grant"),
    app_commands.Choice(name="차감", value="remove"),
])
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(유저="대상 유저", 처리="경고 부여 또는 차감", 사유="조치 사유")
async def warning_command(interaction: discord.Interaction, 유저: discord.Member, 처리: app_commands.Choice[str], 사유: str = "관리자 조치"):
    allowed, message = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, message)
    if 처리.value == "grant":
        add_sanction(interaction.guild_id, 유저.id, "warning", interaction.user.id, 사유)
        action = "경고 1개 부여"
    else:
        if not remove_sanction(interaction.guild_id, 유저.id, "warning", interaction.user.id, 사유):
            return await send_command_error(interaction, "차감할 경고가 없습니다.")
        action = "경고 1개 차감"
    warnings, cautions = db_counts(interaction.guild_id, 유저.id)
    await send_action_dm(유저, interaction.guild, action, 사유, interaction.user.id, warnings, cautions)
    banned = False
    if 처리.value == "grant":
        banned = await apply_warning_threshold(interaction.guild, 유저, interaction.user.id, "경고 5개 누적")
    result = f"{유저.mention} 님에게 {action}했습니다.\n현재 경고 **{warnings}/{WARNING_LIMIT}개**, 주의 **{cautions}/{CAUTION_LIMIT}개**입니다."
    if banned:
        result += "\n경고 5개 누적으로 서버에서 자동 차단했습니다."
    await interaction.response.send_message(result, ephemeral=True)


@bot.tree.command(name="주의", description="유저의 주의를 부여하거나 차감합니다.")
@app_commands.choices(처리=[
    app_commands.Choice(name="부여", value="grant"),
    app_commands.Choice(name="차감", value="remove"),
])
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(유저="대상 유저", 처리="주의 부여 또는 차감", 사유="조치 사유")
async def caution_command(interaction: discord.Interaction, 유저: discord.Member, 처리: app_commands.Choice[str], 사유: str = "관리자 조치"):
    allowed, message = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, message)
    if 처리.value == "grant":
        action, warnings, cautions = await grant_caution(interaction.guild, 유저, interaction.user.id, 사유)
    else:
        if not remove_sanction(interaction.guild_id, 유저.id, "caution", interaction.user.id, 사유):
            return await send_command_error(interaction, "차감할 주의가 없습니다.")
        action = "주의 1개 차감"
        warnings, cautions = db_counts(interaction.guild_id, 유저.id)
    await send_action_dm(유저, interaction.guild, action, 사유, interaction.user.id, warnings, cautions)
    banned = False
    if 처리.value == "grant" and warnings >= WARNING_LIMIT:
        banned = await apply_warning_threshold(interaction.guild, 유저, interaction.user.id, "경고 5개 누적")
    result = f"{유저.mention} 님에게 {action}했습니다.\n현재 경고 **{warnings}/{WARNING_LIMIT}개**, 주의 **{cautions}/{CAUTION_LIMIT}개**입니다."
    if banned:
        result += "\n경고 5개 누적으로 서버에서 자동 차단했습니다."
    await interaction.response.send_message(result, ephemeral=True)


class SanctionHistoryView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], owner_id: int):
        super().__init__(timeout=180)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self.refresh_buttons()

    def refresh_buttons(self):
        self.previous.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("이 조회 화면을 실행한 사용자만 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


@bot.tree.command(name="제재내역", description="본인 또는 지정한 유저의 제재 내역을 조회합니다.")
@app_commands.describe(유저="관리자는 조회할 유저를 지정할 수 있습니다. 비워두면 본인 내역을 조회합니다.")
async def sanction_history(interaction: discord.Interaction, 유저: discord.Member | None = None):
    guild = interaction.guild
    if guild is None:
        return await send_command_error(interaction, "서버 안에서만 사용할 수 있습니다.")
    is_admin = interaction.user.guild_permissions.administrator
    if not is_admin and 유저 is not None and 유저.id != interaction.user.id:
        return await send_command_error(interaction, "일반 유저는 본인의 제재 내역만 조회할 수 있습니다.")
    server_wide = is_admin and 유저 is None
    target_id = 유저.id if is_admin and 유저 is not None else interaction.user.id
    target_member = guild.get_member(target_id)
    target_label = "서버 전체" if server_wide else (target_member.mention if target_member else f"사용자 ID `{target_id}`")
    warnings, cautions = db_counts(guild.id, target_id) if not server_wide else (0, 0)
    with sqlite3.connect(DATABASE_PATH) as conn:
        if is_admin and 유저 is None:
            rows = conn.execute(
                "SELECT user_id,action,reason,actor_id,created_at,automatic FROM moderation_history WHERE guild_id=? ORDER BY created_at DESC LIMIT 100",
                (guild.id,),
            ).fetchall()
            title = "서버 전체 제재 내역"
        else:
            rows = conn.execute(
                "SELECT user_id,action,reason,actor_id,created_at,automatic FROM moderation_history WHERE guild_id=? AND user_id=? ORDER BY created_at DESC LIMIT 100",
                (guild.id, target_id),
            ).fetchall()
            title = f"{target_member.display_name if target_member else '유저'} 제재 내역"
    if not rows:
        embed = make_embed(title, f"{target_label}\n\n기록된 제재 내역이 없습니다.")
        if not server_wide:
            embed.add_field(name="현재 경고 / 주의", value=f"경고 {warnings}/{WARNING_LIMIT}개 · 주의 {cautions}/{CAUTION_LIMIT}개")
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    pages = []
    page_size = 8
    for start in range(0, len(rows), page_size):
        chunk = rows[start:start + page_size]
        summary = f"조회 대상: {target_label}"
        if not server_wide:
            summary += f"\n현재 경고 **{warnings}/{WARNING_LIMIT}개** · 주의 **{cautions}/{CAUTION_LIMIT}개**"
        embed = make_embed(title, summary)
        lines = []
        for user_id, action, reason, actor_id, created_at, automatic in chunk:
            when = datetime.fromtimestamp(created_at, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            actor = "자동 시스템" if automatic or actor_id is None else f"관리자 ID {actor_id}"
            lines.append(f"**{action}** · <t:{int(created_at)}:f>\n대상: <@{user_id}> · 조치자: {actor}\n사유: {(reason or '없음')[:180]}")
        embed.add_field(name=f"기록 {start + 1}~{start + len(chunk)}", value="\n\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"{start // page_size + 1}/{(len(rows) + page_size - 1) // page_size} 페이지 · 최근 최대 100건")
        pages.append(embed)
    if len(pages) == 1:
        return await interaction.response.send_message(embed=pages[0], ephemeral=True)
    view = SanctionHistoryView(pages, interaction.user.id)
    await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)


def make_embed(title: str, description: str = "", color: discord.Color | None = None) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )


def can_bot_moderate(member: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
    me = guild.me
    if me is None:
        return False, "봇의 서버 정보를 확인할 수 없습니다."
    if member.id == guild.owner_id:
        return False, "서버 소유자는 이 작업의 대상이 될 수 없습니다."
    if member.id == me.id:
        return False, "봇 자신에게는 이 작업을 수행할 수 없습니다."
    if member.top_role >= me.top_role:
        return False, "대상 유저의 최고 역할이 봇의 최고 역할보다 높거나 같습니다. 봇 역할을 위로 올려주세요."
    return True, ""


async def send_command_error(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# -------------------- 1. /유저정보 --------------------

@bot.tree.command(name="유저정보", description="선택한 유저의 디스코드 및 서버 정보를 조회합니다.")
@app_commands.describe(유저="정보를 조회할 서버 멤버")
async def user_info(interaction: discord.Interaction, 유저: discord.Member):
    member = 유저
    user = member
    try:
        fetched_user = await bot.fetch_user(member.id)
        user = fetched_user
    except discord.HTTPException:
        pass

    embed = make_embed(f"유저 정보 · {member.display_name}")
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="사용자명", value=f"`{member.name}`", inline=True)
    embed.add_field(name="표시 이름", value=member.global_name or "없음", inline=True)
    embed.add_field(name="서버 닉네임", value=member.nick or "없음", inline=True)
    embed.add_field(name="사용자 ID", value=f"`{member.id}`", inline=False)
    embed.add_field(name="계정 생성일", value=discord.utils.format_dt(member.created_at, style="F"), inline=True)
    embed.add_field(
        name="서버 가입일",
        value=discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "확인 불가",
        inline=True,
    )
    embed.add_field(name="계정 유형", value="봇" if member.bot else "일반 계정", inline=True)

    status = getattr(member, "status", None)
    embed.add_field(name="온라인 상태", value=str(status) if status else "상태 정보 없음", inline=True)

    activities = []
    for activity in getattr(member, "activities", []):
        name = getattr(activity, "name", None)
        details = getattr(activity, "details", None)
        state = getattr(activity, "state", None)
        if name:
            activities.append(f"**{name}**" + (f"\n{details}" if details else "") + (f"\n{state}" if state else ""))
    embed.add_field(
        name="현재 활동",
        value="\n\n".join(activities)[:1024] if activities else "활동 정보 없음",
        inline=False,
    )

    roles = [role.mention for role in reversed(member.roles) if role != interaction.guild.default_role]
    embed.add_field(name=f"역할 ({len(roles)}개)", value=" ".join(roles)[:1024] if roles else "없음", inline=False)
    embed.add_field(name="최고 역할", value=member.top_role.mention, inline=True)
    embed.add_field(name="서버 부스트", value=discord.utils.format_dt(member.premium_since, style="R") if member.premium_since else "아니요", inline=True)

    if member.voice:
        voice_text = f"채널: {member.voice.channel.mention if member.voice.channel else '알 수 없음'}"
        voice_text += f"\n서버 음소거: {'예' if member.voice.mute else '아니요'}"
        voice_text += f"\n서버 헤드폰 음소거: {'예' if member.voice.deaf else '아니요'}"
    else:
        voice_text = "음성 채널에 접속하지 않음"
    embed.add_field(name="음성 상태", value=voice_text, inline=True)

    timeout_until = getattr(member, "timed_out_until", None)
    embed.add_field(
        name="타임아웃",
        value=discord.utils.format_dt(timeout_until, style="R") if timeout_until else "적용되지 않음",
        inline=True,
    )

    perms = []
    for label, attr in [
        ("관리자", "administrator"),
        ("서버 관리", "manage_guild"),
        ("메시지 관리", "manage_messages"),
        ("멤버 추방", "kick_members"),
        ("멤버 차단", "ban_members"),
        ("멤버 타임아웃", "moderate_members"),
        ("역할 관리", "manage_roles"),
        ("채널 관리", "manage_channels"),
    ]:
        if getattr(member.guild_permissions, attr, False):
            perms.append(label)
    embed.add_field(name="주요 서버 권한", value=", ".join(perms) if perms else "특별 권한 없음", inline=False)

    if getattr(user, "banner", None):
        embed.set_image(url=user.banner.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# -------------------- 2. /타임아웃 --------------------

@bot.tree.command(name="타임아웃", description="선택한 유저에게 지정한 시간 동안 타임아웃을 적용합니다.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(시간="분 단위 (1~40320)", 유저="타임아웃할 서버 멤버", 사유="선택 입력")
async def timeout_user(
    interaction: discord.Interaction,
    시간: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
    유저: discord.Member,
    사유: str | None = None,
):
    allowed, reason = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, reason)
    try:
        final_reason = 사유 or f"{interaction.user} 명령어로 타임아웃"
        await 유저.timeout(timedelta(minutes=시간), reason=final_reason)
        add_history(interaction.guild_id, 유저.id, f"타임아웃 ({시간}분)", final_reason, interaction.user.id)
        await send_action_dm(유저, interaction.guild, f"타임아웃 {시간}분", final_reason, interaction.user.id)
        embed = make_embed("타임아웃 적용 완료", f"{유저.mention}에게 **{시간}분** 타임아웃을 적용했습니다.", discord.Color.orange())
        embed.add_field(name="사유", value=사유 or "입력되지 않음", inline=False)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await send_command_error(interaction, "봇에게 타임아웃 권한이 없거나 역할 순서가 올바르지 않습니다.")
    except discord.HTTPException as exc:
        log.exception("타임아웃 적용 실패")
        await send_command_error(interaction, "요청을 처리하지 못했습니다. 봇 권한과 역할 순서를 확인한 뒤 다시 시도해 주세요.")


# -------------------- 3. /삭제 --------------------

@bot.tree.command(name="삭제", description="현재 채널의 최근 메시지를 지정한 개수만큼 삭제합니다.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(개수="삭제할 최근 메시지 개수 (1~1000)")
async def delete_messages(interaction: discord.Interaction, 개수: app_commands.Range[int, 1, MAX_DELETE_COUNT]):
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return await send_command_error(interaction, "이 채널에서는 메시지를 삭제할 수 없습니다.")

    await interaction.response.defer(ephemeral=True)
    try:
        messages = [m async for m in channel.history(limit=개수)]
        if not messages:
            return await interaction.followup.send("삭제할 메시지가 없습니다.", ephemeral=True)

        cutoff = discord.utils.utcnow() - timedelta(days=14)
        recent = [m for m in messages if m.created_at > cutoff]
        old = [m for m in messages if m.created_at <= cutoff]
        deleted_count = 0

        # 최근 메시지는 Discord bulk-delete 제한에 맞춰 100개씩 처리
        for start in range(0, len(recent), 100):
            batch = recent[start:start + 100]
            if len(batch) == 1:
                await batch[0].delete()
                deleted_count += 1
            elif batch:
                await channel.delete_messages(batch)
                deleted_count += len(batch)

        # 14일이 지난 메시지는 개별 삭제
        for message in old:
            try:
                await message.delete()
                deleted_count += 1
            except discord.NotFound:
                pass

        await interaction.followup.send(f"최근 메시지 **{deleted_count}개**를 삭제했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("봇에게 메시지 관리 권한이 없습니다.", ephemeral=True)
    except discord.HTTPException as exc:
        log.exception("메시지 삭제 실패")
        await interaction.followup.send("메시지를 삭제하지 못했습니다. 권한을 확인한 뒤 다시 시도해 주세요.", ephemeral=True)


# -------------------- 4. /추방 --------------------

@bot.tree.command(name="추방", description="선택한 유저를 서버에서 추방합니다.")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(유저="추방할 서버 멤버", 사유="선택 입력")
async def kick_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str | None = None):
    allowed, reason = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, reason)
    try:
        final_reason = 사유 or f"{interaction.user} 명령어로 추방"
        await 유저.kick(reason=final_reason)
        add_history(interaction.guild_id, 유저.id, "추방", final_reason, interaction.user.id)
        await send_action_dm(유저, interaction.guild, "추방", final_reason, interaction.user.id)
        embed = make_embed("추방 완료", f"**{유저}** 님을 서버에서 추방했습니다.", discord.Color.orange())
        embed.add_field(name="사유", value=사유 or "입력되지 않음", inline=False)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await send_command_error(interaction, "봇에게 멤버 추방 권한이 없거나 역할 순서가 올바르지 않습니다.")
    except discord.HTTPException as exc:
        log.exception("멤버 추방 실패")
        await send_command_error(interaction, "요청을 처리하지 못했습니다. 봇 권한과 역할 순서를 확인해 주세요.")


# -------------------- 5. /차단 --------------------

@bot.tree.command(name="차단", description="선택한 유저를 서버에서 차단합니다.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(유저="차단할 서버 멤버", 사유="선택 입력")
async def ban_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str | None = None):
    allowed, reason = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, reason)
    try:
        final_reason = 사유 or f"{interaction.user} 명령어로 차단"
        await interaction.guild.ban(유저, reason=final_reason, delete_message_seconds=0)
        add_history(interaction.guild_id, 유저.id, "차단", final_reason, interaction.user.id)
        await send_action_dm(유저, interaction.guild, "차단", final_reason, interaction.user.id)
        embed = make_embed("서버 차단 완료", f"**{유저}** 님을 서버에서 차단했습니다.", discord.Color.red())
        embed.add_field(name="사유", value=사유 or "입력되지 않음", inline=False)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await send_command_error(interaction, "봇에게 멤버 차단 권한이 없거나 역할 순서가 올바르지 않습니다.")
    except discord.HTTPException as exc:
        log.exception("멤버 차단 실패")
        await send_command_error(interaction, "요청을 처리하지 못했습니다. 봇 권한과 역할 순서를 확인해 주세요.")


# -------------------- 6. /공지 --------------------

@bot.tree.command(name="공지", description="서버 공지 채널에 @everyone과 함께 공지를 보냅니다.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(내용="공지로 보낼 메시지")
async def announce(interaction: discord.Interaction, 내용: str):
    if not 내용.strip() or len(내용) > 1980:
        return await send_command_error(interaction, "공지 내용은 1~1980자 사이로 입력해주세요.")
    guild = interaction.guild
    channel = guild.public_updates_channel or guild.system_channel

    if channel is None:
        return await send_command_error(
            interaction,
            "공지 채널을 자동으로 찾지 못했습니다. 커뮤니티 서버의 공개 업데이트 채널 또는 시스템 채널을 설정해주세요.",
        )

    me = guild.me
    perms = channel.permissions_for(me) if me else None
    if not perms or not perms.send_messages:
        return await send_command_error(interaction, "봇이 감지한 공지 채널에 메시지를 보낼 권한이 없습니다.")

    try:
        await channel.send(
            f"@everyone\n{내용}",
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
        )
        await interaction.response.send_message(f"{channel.mention}에 공지를 발송했습니다.", ephemeral=True)
    except discord.Forbidden:
        await send_command_error(interaction, "공지 채널에 메시지를 보내거나 @everyone을 멘션할 권한이 없습니다.")
    except discord.HTTPException as exc:
        log.exception("공지 발송 실패")
        await send_command_error(interaction, "공지를 보내지 못했습니다. 채널 권한을 확인한 뒤 다시 시도해 주세요.")


# -------------------- 7. 들낙 감지 --------------------

async def was_removed_by_kick(guild: discord.Guild, user_id: int) -> bool | None:
    """True=추방 확인, False=추방 아님, None=감사 로그 확인 불가."""
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None
    try:
        # Discord 감사 로그 반영 지연을 고려
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=1.5))
        async for entry in guild.audit_logs(
            limit=12,
            action=discord.AuditLogAction.kick,
        ):
            if entry.target and getattr(entry.target, "id", None) == user_id:
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if 0 <= age <= 15:
                    return True
        return False
    except (discord.Forbidden, discord.HTTPException):
        return None


@bot.event
async def on_member_join(member: discord.Member):
    if leave_guard_enabled(member.guild.id):
        record_member_join(
            member.guild.id,
            member.id,
            member.joined_at.timestamp() if member.joined_at else discord.utils.utcnow().timestamp(),
        )


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    if not leave_guard_enabled(guild.id):
        clear_member_join(guild.id, member.id)
        return

    joined_at = get_member_join(guild.id, member.id)
    clear_member_join(guild.id, member.id)
    if joined_at is None:
        return

    elapsed = discord.utils.utcnow().timestamp() - joined_at
    if elapsed < 0 or elapsed > LEAVE_WINDOW.total_seconds():
        return

    # 관리자가 추방한 경우는 자동 차단 대상에서 제외
    kicked = await was_removed_by_kick(guild, member.id)
    if kicked is True or kicked is None:
        # 감사 로그 확인 권한이 없거나 조회에 실패한 경우 오차단 방지를 위해 조치하지 않음
        return

    me = guild.me
    if me is None or not me.guild_permissions.ban_members:
        return
    if member.id == guild.owner_id:
        return

    try:
        await guild.ban(
            member,
            reason="들낙 감지: 입장 후 24시간 이내 자진 퇴장",
            delete_message_seconds=0,
        )
        add_history(guild.id, member.id, "들낙 자동 차단", "입장 후 24시간 이내 자진 퇴장", None, automatic=True)
        await send_action_dm(member, guild, "들낙 감지 자동 차단", "입장 후 24시간 이내 자진 퇴장", None)
        log.info("들낙 자동 차단: guild=%s user=%s", guild.id, member.id)
        notice_channel = guild.system_channel or guild.public_updates_channel
        if notice_channel:
            try:
                await notice_channel.send(
                    f"들낙 감지에 따라 **{member}** 님을 서버에서 차단했습니다.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
    except discord.Forbidden:
        log.warning("들낙 자동 차단 권한 부족: guild=%s user=%s", guild.id, member.id)
    except discord.HTTPException:
        log.exception("들낙 자동 차단 처리 실패: guild=%s user=%s", guild.id, member.id)


# -------------------- 8. 자동 도배 감지 --------------------

def normalize_text(text: str) -> str:
    return " ".join(text.split()).casefold()


async def get_message_signatures(message: discord.Message) -> set[str]:
    signatures: set[str] = set()
    normalized = normalize_text(message.content)
    if normalized:
        signatures.add("text:" + normalized)

    for attachment in message.attachments:
        content_type = attachment.content_type or ""
        filename = attachment.filename.lower()
        is_image = content_type.startswith("image/") or filename.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        )
        if not is_image:
            continue
        try:
            data = await attachment.read()
            digest = hashlib.sha256(data).hexdigest()
            signatures.add("image:" + digest)
        except (discord.HTTPException, discord.NotFound):
            # 이미지를 읽지 못한 경우 파일 URL을 대체 식별자로 사용
            signatures.add("image-url:" + attachment.url)
    return signatures


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot or message.webhook_id:
        return

    member = message.author
    if not isinstance(member, discord.Member):
        return

    # 관리자/메시지 관리 권한자는 자동 도배 제재에서 제외
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return

    # 타임아웃 권한/역할 순서를 갖추지 못하면 감지하더라도 제재할 수 없음
    me = message.guild.me
    if me is None or not me.guild_permissions.moderate_members:
        return
    if member.id == message.guild.owner_id or member.top_role >= me.top_role:
        return
    if member.is_timed_out():
        return

    signatures = await get_message_signatures(message)
    if not signatures:
        return

    now = discord.utils.utcnow()
    key = (message.guild.id, member.id)
    state = spam_states.get(key)

    if (
        state
        and (now - state["last_at"]).total_seconds() <= SPAM_WINDOW_SECONDS
        and (now - state["first_at"]).total_seconds() <= SPAM_WINDOW_SECONDS
        and signatures.intersection(state["signatures"])
    ):
        state["count"] += 1
        state["last_at"] = now
        state["signatures"] = signatures
    else:
        state = {
            "count": 1,
            "first_at": now,
            "last_at": now,
            "signatures": signatures,
        }
        spam_states[key] = state

    if state["count"] >= SPAM_LIMIT:
        try:
            await member.timeout(
                timedelta(minutes=TIMEOUT_MINUTES),
                reason=f"자동 도배 감지: {SPAM_WINDOW_SECONDS}초 이내 동일 텍스트/이미지 {state['count']}회 연속 발송",
            )
            spam_states.pop(key, None)
            spam_reason = f"{SPAM_WINDOW_SECONDS}초 이내 동일 텍스트 또는 이미지 {state['count']}회 연속 발송"
            add_history(message.guild.id, member.id, f"자동 타임아웃 ({TIMEOUT_MINUTES}분)", spam_reason, None, automatic=True)
            await send_action_dm(member, message.guild, f"도배 감지 타임아웃 {TIMEOUT_MINUTES}분", spam_reason, None)
            notice = make_embed(
                "자동 도배 감지",
                f"{member.mention} 님이 동일한 텍스트 또는 이미지를 반복 발송하여 **{TIMEOUT_MINUTES}분 타임아웃** 처리되었습니다.",
                discord.Color.orange(),
            )
            try:
                await message.channel.send(
                    embed=notice,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            log.info("자동 타임아웃: guild=%s user=%s", message.guild.id, member.id)
        except discord.Forbidden:
            log.warning("자동 타임아웃 권한 부족: guild=%s user=%s", message.guild.id, member.id)
            spam_states.pop(key, None)
        except discord.HTTPException:
            log.exception("자동 타임아웃 실패: guild=%s user=%s", message.guild.id, member.id)
            spam_states.pop(key, None)

    # 다른 봇 명령어 처리 등 commands.Bot 기본 처리를 유지
    await bot.process_commands(message)


# -------------------- 공통 오류 처리 --------------------

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "이 명령어를 사용할 서버 권한이 없습니다."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "봇에게 필요한 권한이 없습니다. 봇 역할과 채널 권한을 확인해주세요."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = "잠시 후 다시 시도해주세요."
    else:
        log.exception("슬래시 명령어 오류", exc_info=error)
        msg = "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."

    await send_command_error(interaction, msg)


@bot.event
async def on_ready():
    if not caution_expiry_loop.is_running():
        caution_expiry_loop.start()
    log.info("로그인 완료: %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    log.info("접속 중인 서버 수: %s", len(bot.guilds))
    # setup_hook 시점에는 서버 캐시가 아직 비어 있을 수 있어 on_ready에서 길드별 동기화
    if not getattr(bot, "_guild_commands_synced", False):
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                log.info("서버 명령어 동기화 완료: %s (%s개)", guild.name, len(synced))
            except Exception:
                log.exception("서버 명령어 동기화 실패: %s", guild.id)
        bot._guild_commands_synced = True


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN 환경변수가 없습니다.\n"
            "Windows PowerShell: $env:DISCORD_TOKEN='봇_토큰'; py bot.py"
        )
    bot.run(TOKEN)
