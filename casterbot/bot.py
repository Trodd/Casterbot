"""Main bot logic and sync loop."""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import config, db, sheets
from .views import ClaimView, CloseChannelView

# Set up logging to both console and file
log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
log_dir = Path(__file__).resolve().parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "casterbot.log"

# Create handlers
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(log_format))

file_handler = RotatingFileHandler(
    log_file,
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=3,  # Keep 3 backup files
    encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(log_format))

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[console_handler, file_handler],
)
log = logging.getLogger("casterbot")

# Quiet discord.py's noisy gateway logs
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)


class CasterBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await db.init_db()
        log.info("Database initialized")

        # Register persistent views for existing matches
        await self._register_persistent_views()

        # Sync slash commands
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("Slash commands synced")

        # Start background sync loop
        self.sync_matches_loop.start()

    async def _register_persistent_views(self) -> None:
        """Re-register views for matches that have claim messages."""
        async with __import__("aiosqlite").connect(config.DB_PATH) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            cursor = await conn.execute(
                "SELECT match_id FROM matches WHERE message_id IS NOT NULL"
            )
            rows = await cursor.fetchall()
        for row in rows:
            self.add_view(ClaimView(row["match_id"]))
        log.info(f"Registered {len(rows)} persistent claim views")
        
        # Register close channel views for private channels
        async with __import__("aiosqlite").connect(config.DB_PATH) as conn:
            conn.row_factory = __import__("aiosqlite").Row
            cursor = await conn.execute(
                "SELECT match_id FROM matches WHERE private_channel_id IS NOT NULL"
            )
            rows = await cursor.fetchall()
        for row in rows:
            self.add_view(CloseChannelView(row["match_id"]))
            self.add_view(CloseChannelView(row["match_id"], confirming=True))
        log.info(f"Registered {len(rows)} persistent close channel views")

    async def on_ready(self) -> None:
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")

    @tasks.loop(seconds=config.SYNC_INTERVAL_SECONDS)
    async def sync_matches_loop(self) -> None:
        await self.wait_until_ready()
        try:
            await sync_matches(self)
        except Exception as e:
            log.exception(f"Sync loop error: {e}")

    @sync_matches_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.wait_until_ready()
        # Run once immediately
        await sync_matches(self)


async def sync_matches(bot: CasterBot) -> int:
    """Fetch matches from sheet, insert new ones, post claim messages. Returns count of new messages."""
    log.debug("Syncing matches from sheet...")
    matches = await sheets.fetch_upcoming_matches()
    log.debug(f"Fetched {len(matches)} upcoming matches")

    existing_matches = await db.get_matches_with_message()

    # Get current match IDs from sheet
    sheet_match_ids = {m.match_id for m in matches}

    # Delete messages for matches no longer in sheet
    channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
    
    matches_to_delete = [m for m in existing_matches if m["match_id"] not in sheet_match_ids]
    if matches_to_delete:
        log.info(f"Found {len(matches_to_delete)} matches to remove (no longer in sheet)")
        for m in matches_to_delete:
            log.info(f"  - Will delete: {m['team_a']} vs {m['team_b']} (ID: {m['match_id'][:40]}...)")
    
    for match in existing_matches:
        if match["match_id"] not in sheet_match_ids:
            # Match was removed from sheet (outdated)
            # Keep match + claim message if there's still an active private channel
            if match.get("private_channel_id"):
                log.info(f"Keeping match + claim message for {match['team_a']} vs {match['team_b']} - private channel still active")
                continue
            
            # No private channel, safe to delete entirely
            if channel and match.get("message_id"):
                try:
                    msg = await channel.fetch_message(match["message_id"])
                    await msg.delete()
                    log.info(f"Deleted message for removed match: {match['team_a']} vs {match['team_b']}")
                except discord.NotFound:
                    log.debug(f"Message already deleted for match: {match['match_id']}")
                except Exception as e:
                    log.error(f"Failed to delete message for {match['match_id']}: {e}")
            await db.delete_match(match["match_id"])
            log.info(f"Removed match from DB: {match['team_a']} vs {match['team_b']}")
        else:
            # Match still on sheet - verify message still exists
            if channel and match.get("message_id"):
                try:
                    await channel.fetch_message(match["message_id"])
                except discord.NotFound:
                    # Message was deleted externally, clear ID so it gets reposted
                    await db.clear_message_id(match["match_id"])
                    log.info(f"Message missing for {match['team_a']} vs {match['team_b']}, will repost")
                except Exception as e:
                    log.error(f"Error checking message for {match['match_id']}: {e}")

    new_count = 0
    for m in matches:
        inserted = await db.upsert_match(
            match_id=m.match_id,
            team_a=m.team_a,
            team_b=m.team_b,
            match_date=m.match_date,
            match_time=m.match_time,
            match_timestamp=int(m.match_datetime.timestamp()),
            match_type=m.match_type,
        )
        if inserted:
            new_count += 1

    # Post messages for matches without one
    pending = await db.get_matches_without_message()
    if not channel:
        log.warning(f"Claim channel {config.CLAIM_CHANNEL_ID} not found")
        return new_count

    for match in pending:
        claims = await db.get_claims(match["match_id"])
        view = ClaimView(match["match_id"], match, claims)
        bot.add_view(view)
        try:
            msg = await channel.send(view=view)
            await db.set_message_id(match["match_id"], msg.id, channel.id)
            log.info(f"Posted claim message for {match['team_a']} vs {match['team_b']}")
        except Exception as e:
            log.error(f"Failed to post claim message: {e}")

    return new_count


# Slash commands
bot_instance: CasterBot | None = None


def get_bot() -> CasterBot:
    global bot_instance
    if bot_instance is None:
        bot_instance = CasterBot()
        _register_commands(bot_instance)
    return bot_instance


def _register_commands(bot: CasterBot) -> None:
    @bot.tree.command(name="sync_matches", description="Manually sync upcoming matches from the sheet")
    async def cmd_sync_matches(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        count = await sync_matches(bot)
        await interaction.followup.send(f"Sync complete. {count} new matches added.", ephemeral=True)

    @bot.tree.command(name="match_status", description="Show claim status for a match")
    @app_commands.describe(match_id="The match ID number (shown on claim message)")
    async def cmd_match_status(interaction: discord.Interaction, match_id: int):
        match = await db.get_match_by_simple_id(match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        claims = await db.get_claims(match["match_id"])
        view = ClaimView(match["match_id"], match, claims)
        await interaction.response.send_message(view=view, ephemeral=True)

    @bot.tree.command(name="force_channel", description="Force create the private channel for a match (admin)")
    @app_commands.describe(match_id="The match ID number")
    async def cmd_force_channel(interaction: discord.Interaction, match_id: int):
        match = await db.get_match_by_simple_id(match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        if match.get("private_channel_id"):
            await interaction.response.send_message(
                f"Channel already exists: <#{match['private_channel_id']}>", ephemeral=True
            )
            return
        claims = await db.get_claims(match["match_id"])
        from .views import create_private_match_channel
        channel = await create_private_match_channel(interaction, match, claims)
        if channel:
            await interaction.response.send_message(f"Created <#{channel.id}>", ephemeral=True)
        else:
            await interaction.response.send_message("Failed to create channel.", ephemeral=True)

    @bot.tree.command(name="refresh_messages", description="Refresh all claim messages (updates UI)")
    async def cmd_refresh_messages(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        matches = await db.get_matches_with_message()
        channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
        if not channel:
            await interaction.followup.send("Claim channel not found.", ephemeral=True)
            return
        
        updated = 0
        for match in matches:
            if not match.get("message_id"):
                continue
            try:
                msg = await channel.fetch_message(match["message_id"])
                claims = await db.get_claims(match["match_id"])
                new_view = ClaimView(match["match_id"], match, claims)
                await msg.edit(view=new_view)
                updated += 1
            except discord.NotFound:
                log.info(f"Message not found for {match['match_id']}")
            except Exception as e:
                log.error(f"Failed to refresh message for {match['match_id']}: {e}")
        
        await interaction.followup.send(f"Refreshed {updated} claim messages.", ephemeral=True)

    @bot.tree.command(name="manage_claim", description="Add or remove a user from a match slot (admin)")
    @app_commands.describe(
        match_id="The match ID number (shown on claim message)",
        action="Add or remove a user",
        role="Role type",
        slot="Slot number (1-2 for caster, 1 for camop/sideline)",
        user="User to add (required for Add action)"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Add", value="add"),
            app_commands.Choice(name="Remove", value="remove"),
        ],
        role=[
            app_commands.Choice(name="Caster", value="caster"),
            app_commands.Choice(name="Cam Op", value="camop"),
            app_commands.Choice(name="Sideline", value="sideline"),
        ]
    )
    async def cmd_manage_claim(interaction: discord.Interaction, match_id: int, action: str, role: str, slot: int, user: discord.Member | None = None):
        match = await db.get_match_by_simple_id(match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        
        internal_match_id = match["match_id"]
        
        # Validate slot number
        from .views import MAX_CASTERS, MAX_CAMOPS, MAX_SIDELINE
        max_slot = MAX_CASTERS if role == "caster" else (MAX_CAMOPS if role == "camop" else MAX_SIDELINE)
        if slot < 1 or slot > max_slot:
            await interaction.response.send_message(f"Invalid slot. {role} has slots 1-{max_slot}.", ephemeral=True)
            return
        
        role_display = role.capitalize() if role != "camop" else "Cam Op"
        
        if action == "add":
            if not user:
                await interaction.response.send_message("You must specify a user to add.", ephemeral=True)
                return
            # Add user to slot
            previous = await db.claim_slot(internal_match_id, user.id, role, slot)
            if previous:
                result_msg = f"Added {user.mention} as {role_display} {slot} (replaced <@{previous}>)."
            else:
                result_msg = f"Added {user.mention} as {role_display} {slot}."
        else:
            # Remove current holder
            removed_user = await db.remove_claim_by_slot(internal_match_id, role, slot)
            if not removed_user:
                await interaction.response.send_message("That slot is already empty.", ephemeral=True)
                return
            result_msg = f"Removed <@{removed_user}> from {role_display} {slot}."
        
        # Refresh the claim message
        channel = bot.get_channel(config.CLAIM_CHANNEL_ID)
        if channel and match.get("message_id"):
            try:
                msg = await channel.fetch_message(match["message_id"])
                claims = await db.get_claims(internal_match_id)
                new_view = ClaimView(internal_match_id, match, claims)
                await msg.edit(view=new_view)
            except Exception as e:
                log.error(f"Failed to refresh message: {e}")
        
        await interaction.response.send_message(result_msg, ephemeral=True)

    @bot.tree.command(name="margarita", description="Request a margarita from the margarita machine")
    async def cmd_margarita(interaction: discord.Interaction):
        import random
        flavors = ["Classic Lime", "Strawberry", "Mango", "Spicy Jalapeño", "Blue Curaçao", "Watermelon", "Peach", "Pineapple", "Coconut", "Passion Fruit", "Blood Orange", "Blackberry"]
        flavor = random.choice(flavors)
        responses = [
            f"🍹 **WHIRRRR** The margarita machine dispenses a fresh {flavor} margarita for {interaction.user.mention}!",
            f"🧊 Ice crushed. Tequila poured. Lime squeezed. {interaction.user.mention} receives a {flavor} margarita! 🍹",
            f"🍹 One {flavor} margarita, coming right up! *slides drink across the bar to {interaction.user.mention}*",
            f"⚠️ ERROR: Margarita machine is out of... just kidding! 🍹 Here's your {flavor} margarita, {interaction.user.mention}!",
            f"🎰 *slot machine noises* 🍹🍹🍹 JACKPOT! {interaction.user.mention} wins a {flavor} margarita!",
            f"🤖 BEEP BOOP. Margarita protocol initiated. Dispensing {flavor} margarita to {interaction.user.mention}. 🍹",
            f"🌴 A tiny beach umbrella pops out, followed by a {flavor} margarita for {interaction.user.mention}! 🍹🏖️",
            f"🍹 The margarita machine hums approvingly and presents {interaction.user.mention} with a perfectly crafted {flavor} margarita.",
            f"🎵 *Margaritaville plays softly* 🍹 {interaction.user.mention}, your {flavor} margarita awaits!",
            f"🧪 After careful scientific analysis... the optimal drink for {interaction.user.mention} is a {flavor} margarita! 🍹",
            # Echo Arena themed
            f"🥏 {interaction.user.mention} catches the disc— wait no, it's a {flavor} margarita! 🍹",
            f"💥 STUNNED! {interaction.user.mention} gets hit with a {flavor} margarita to the face! 🍹",
            f"🚀 {interaction.user.mention} launches out of the tube and grabs a {flavor} margarita mid-flight! 🍹",
            f"🎯 GOAL! {interaction.user.mention} scores a {flavor} margarita! The crowd goes wild! 🍹",
            f"⚡ {interaction.user.mention} jousts the margarita machine and wins a {flavor} margarita! 🍹",
            f"🔄 Self-pass into regrab! {interaction.user.mention} secures the {flavor} margarita! 🍹",
            f"🍹 {interaction.user.mention} boosts off the wall and intercepts a {flavor} margarita!",
            f"🎮 **OVERTIME!** {interaction.user.mention} clutches it with a {flavor} margarita! 🍹",
            f"🛸 {interaction.user.mention} floats through zero-g and catches a {flavor} margarita drifting by. 🍹",
            f"💫 {interaction.user.mention} arcs a beautiful {flavor} margarita right into their own hands! 🍹",
            f"🧤 Clean catch! {interaction.user.mention} grabs the {flavor} margarita out of the air! 🍹",
            f"🏆 ESL Champion {interaction.user.mention} is awarded a ceremonial {flavor} margarita! 🍹",
            f"🤖 RAD clears {interaction.user.mention} for a {flavor} margarita. No headbutting detected. 🍹",
        ]
        await interaction.response.send_message(random.choice(responses))

    @bot.tree.command(name="leaderboard", description="Show the caster leaderboard (including cam ops)")
    async def cmd_leaderboard(interaction: discord.Interaction):
        leaderboard = await db.get_caster_leaderboard(limit=10)
        if not leaderboard:
            await interaction.response.send_message("No casts recorded yet!", ephemeral=True)
            return
        
        lines = ["# 🎙️ Caster Leaderboard\n"]
        for i, entry in enumerate(leaderboard, start=1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"**{i}.**"
            lines.append(f"{medal} <@{entry['user_id']}> — **{entry['cast_count']}** casts")
        
        # Show the requesting user's rank if not in top 10
        user_count = await db.get_user_cast_count(interaction.user.id)
        if user_count > 0:
            user_in_top = any(e['user_id'] == interaction.user.id for e in leaderboard)
            if not user_in_top:
                lines.append(f"\n---\n**Your casts:** {user_count}")
        
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="set_week", description="Set the current season and week number")
    @app_commands.describe(
        season="Season number or name (e.g., '5' or 'S5')",
        week="Week number (e.g., '3')"
    )
    async def cmd_set_week(interaction: discord.Interaction, season: str, week: str):
        await db.set_setting("season", season)
        await db.set_setting("week", week)
        await interaction.response.send_message(
            f"Updated to **Season {season}, Week {week}**", ephemeral=True
        )

    @bot.tree.command(name="reset_leaderboard", description="Reset the caster leaderboard (admin)")
    async def cmd_reset_leaderboard(interaction: discord.Interaction):
        count = await db.reset_leaderboard()
        await interaction.response.send_message(
            f"Leaderboard reset. Cleared {count} entries.", ephemeral=True
        )


def run() -> None:
    bot = get_bot()
    bot.run(config.DISCORD_TOKEN)
