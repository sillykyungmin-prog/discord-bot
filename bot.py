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
  Manage Messages, Moderate Members, Kick Members, Ban Members
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
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

# -------------------- 설정 --------------------

TOKEN = os.getenv("DISCORD_TOKEN")
TIMEOUT_MINUTES = 10
SPAM_LIMIT = 5
SPAM_WINDOW_SECONDS = 180
MAX_TIMEOUT_MINUTES = 28 * 24
MAX_DELETE_COUNT = 1000

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
        await 유저.timeout(timedelta(minutes=시간), reason=사유 or f"{interaction.user} 명령어로 타임아웃")
        embed = make_embed("타임아웃 적용 완료", f"{유저.mention}에게 **{시간}분** 타임아웃을 적용했습니다.", discord.Color.orange())
        embed.add_field(name="사유", value=사유 or "입력되지 않음", inline=False)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await send_command_error(interaction, "봇에게 타임아웃 권한이 없거나 역할 순서가 올바르지 않습니다.")
    except discord.HTTPException as exc:
        await send_command_error(interaction, f"타임아웃 적용에 실패했습니다: {exc}")


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
        await interaction.followup.send(f"메시지 삭제 중 오류가 발생했습니다: {exc}", ephemeral=True)


# -------------------- 4. /추방 --------------------

@bot.tree.command(name="추방", description="선택한 유저를 서버에서 추방합니다.")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(유저="추방할 서버 멤버", 사유="선택 입력")
async def kick_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str | None = None):
    allowed, reason = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, reason)
    try:
        await 유저.kick(reason=사유 or f"{interaction.user} 명령어로 추방")
        embed = make_embed("추방 완료", f"**{유저}** 님을 서버에서 추방했습니다.", discord.Color.orange())
        embed.add_field(name="사유", value=사유 or "입력되지 않음", inline=False)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await send_command_error(interaction, "봇에게 멤버 추방 권한이 없거나 역할 순서가 올바르지 않습니다.")
    except discord.HTTPException as exc:
        await send_command_error(interaction, f"추방에 실패했습니다: {exc}")


# -------------------- 5. /차단 --------------------

@bot.tree.command(name="차단", description="선택한 유저를 서버에서 차단합니다.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(유저="차단할 서버 멤버", 사유="선택 입력")
async def ban_user(interaction: discord.Interaction, 유저: discord.Member, 사유: str | None = None):
    allowed, reason = can_bot_moderate(유저, interaction.guild)
    if not allowed:
        return await send_command_error(interaction, reason)
    try:
        await interaction.guild.ban(유저, reason=사유 or f"{interaction.user} 명령어로 차단", delete_message_seconds=0)
        embed = make_embed("서버 차단 완료", f"**{유저}** 님을 서버에서 차단했습니다.", discord.Color.red())
        embed.add_field(name="사유", value=사유 or "입력되지 않음", inline=False)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await send_command_error(interaction, "봇에게 멤버 차단 권한이 없거나 역할 순서가 올바르지 않습니다.")
    except discord.HTTPException as exc:
        await send_command_error(interaction, f"차단에 실패했습니다: {exc}")


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
        await send_command_error(interaction, f"공지 발송에 실패했습니다: {exc}")


# -------------------- 7. 자동 도배 감지 --------------------

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
        msg = "명령어 실행 중 예상하지 못한 오류가 발생했습니다. 봇 콘솔 로그를 확인해주세요."

    await send_command_error(interaction, msg)


@bot.event
async def on_ready():
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
