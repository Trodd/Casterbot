"""Discord UI components for claim buttons."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import StringIO

import discord
from dateutil import tz as dateutil_tz
from discord import ButtonStyle, Interaction
from discord.ui import ActionRow, Button, Container, LayoutView, TextDisplay

from . import config, db


MAX_CASTERS = 2
MAX_CAMOPS = 1
MAX_SIDELINE = 1


def _role_allowed(interaction: Interaction) -> bool:
    """Check if user is allowed to claim (if role restriction enabled)."""
    if not config.REQUIRE_CLAIM_ROLE:
        return True
    if not config.CLAIM_ELIGIBLE_ROLE_ID:
        return True
    member = interaction.user
    if isinstance(member, discord.Member):
        return any(r.id == config.CLAIM_ELIGIBLE_ROLE_ID for r in member.roles)
    return False


def _format_match_time(match: dict) -> str:
    """Format match time as Discord timestamp or fallback to text."""
    ts = match.get('match_timestamp')
    if ts:
        # F = full date/time, R = relative
        return f"<t:{ts}:F> (<t:{ts}:R>)"
    return f"{match['match_date']} {match['match_time']}"


def _build_claim_text(match: dict | None, claims: list[dict]) -> str:
    """Build the text content for the claim container."""
    if not match:
        return "**Match not found**"

    lines = [
        f"# `{match['team_a']}` ⚔️ `{match['team_b']}`",
        f"**When:** {_format_match_time(match)}",
        f"**Match ID:** `{match.get('simple_id', '?')}`",
        "",
    ]

    # Caster slots
    for slot in range(1, MAX_CASTERS + 1):
        claim = next(
            (c for c in claims if c["role"] == "caster" and c["slot"] == slot), None
        )
        if claim:
            lines.append(f"Caster {slot}: <@{claim['user_id']}>")
        else:
            lines.append(f"Caster {slot}: *Open*")

    # Cam-op slot
    cam_claim = next((c for c in claims if c["role"] == "camop" and c["slot"] == 1), None)
    if cam_claim:
        lines.append(f"Cam Op: <@{cam_claim['user_id']}>")
    else:
        lines.append("Cam Op: *Open*")

    # Sideline slot
    sideline_claim = next((c for c in claims if c["role"] == "sideline" and c["slot"] == 1), None)
    if sideline_claim:
        lines.append(f"Sideline: <@{sideline_claim['user_id']}>")
    else:
        lines.append("Sideline: *Open*")

    return "\n".join(lines)


class ClaimView(LayoutView):
    """Persistent layout view with Caster 1-2, Cam Op buttons inside a container."""

    def __init__(self, match_id: str, match: dict | None = None, claims: list[dict] | None = None):
        super().__init__(timeout=None)
        self.match_id = match_id
        
        # Build text content
        content = _build_claim_text(match, claims or [])
        
        # Create buttons for row 1 (casters)
        caster_buttons = []
        for slot in range(1, MAX_CASTERS + 1):
            btn = Button(
                style=ButtonStyle.primary,
                label=f"Caster {slot}",
                custom_id=f"claim:caster:{slot}:{match_id}",
            )
            caster_buttons.append(btn)
        
        # Cam-op button
        cam_btn = Button(
            style=ButtonStyle.secondary,
            label="Cam Op",
            custom_id=f"claim:camop:1:{match_id}",
        )
        caster_buttons.append(cam_btn)
        
        # Sideline button
        sideline_btn = Button(
            style=ButtonStyle.secondary,
            label="Sideline",
            custom_id=f"claim:sideline:1:{match_id}",
        )
        caster_buttons.append(sideline_btn)
        
        # Row 2 buttons
        unclaim_btn = Button(
            style=ButtonStyle.danger,
            label="Unclaim",
            custom_id=f"unclaim:{match_id}",
        )
        create_channel_btn = Button(
            style=ButtonStyle.success,
            label="Create Channel",
            custom_id=f"create_channel:{match_id}",
        )
        ready_btn = Button(
            style=ButtonStyle.success,
            label="Crew Ready",
            custom_id=f"crew_ready:{match_id}",
        )
        go_live_btn = Button(
            style=ButtonStyle.primary,
            label="Go Live",
            custom_id=f"go_live:{match_id}",
        )
        
        # Build container with text and action rows
        container = Container(
            TextDisplay(content),
            ActionRow(*caster_buttons),
            ActionRow(unclaim_btn, create_channel_btn, ready_btn, go_live_btn),
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Route interactions to the appropriate handler."""
        custom_id = interaction.data.get("custom_id", "")
        
        if custom_id.startswith("claim:"):
            parts = custom_id.split(":")
            if len(parts) >= 4:
                role, slot = parts[1], int(parts[2])
                await self._handle_claim(interaction, role, slot)
                return False
        elif custom_id.startswith("unclaim:"):
            await self._handle_unclaim(interaction)
            return False
        elif custom_id.startswith("create_channel:"):
            await self._handle_create_channel(interaction)
            return False
        elif custom_id.startswith("crew_ready:"):
            await self._handle_crew_ready(interaction)
            return False
        elif custom_id.startswith("go_live:"):
            await self._handle_go_live(interaction)
            return False
        
        return True

    async def _handle_claim(self, interaction: Interaction, role: str, slot: int):
        if not _role_allowed(interaction):
            await interaction.response.send_message(
                "You don't have the required role to claim.", ephemeral=True
            )
            return
        previous_holder = await db.claim_slot(
            self.match_id, interaction.user.id, role, slot
        )
        if role == "caster":
            slot_name = f"Caster {slot}"
        elif role == "camop":
            slot_name = "Cam Op"
        else:
            slot_name = "Sideline"
        if previous_holder is None:
            await interaction.response.send_message(
                f"You claimed **{slot_name}**!", ephemeral=True
            )
        elif previous_holder == interaction.user.id:
            await interaction.response.send_message(
                f"You already have **{slot_name}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"You took over **{slot_name}** from <@{previous_holder}>!", ephemeral=True
            )
        await self._refresh_message(interaction)

    async def _handle_unclaim(self, interaction: Interaction):
        claims = await db.get_claims(self.match_id)
        user_claims = [c for c in claims if c["user_id"] == interaction.user.id]
        if not user_claims:
            await interaction.response.send_message(
                "You haven't claimed any slots.", ephemeral=True
            )
            return
        for c in user_claims:
            await db.unclaim_slot(self.match_id, c["user_id"], c["role"], c["slot"])
        await interaction.response.send_message("Your claims have been removed.", ephemeral=True)
        await self._refresh_message(interaction)

    async def _handle_create_channel(self, interaction: Interaction):
        """Create the private match channel."""
        match = await db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Could not access server.", ephemeral=True)
            return

        # Check if channel already exists
        if match.get("private_channel_id"):
            existing_channel = guild.get_channel(match["private_channel_id"])
            if existing_channel:
                await interaction.response.send_message(
                    f"Channel already exists: <#{match['private_channel_id']}>", ephemeral=True
                )
                return
            await db.clear_private_channel(self.match_id)

        claims = await db.get_claims(self.match_id)
        casters = [c for c in claims if c["role"] == "caster"]
        camops = [c for c in claims if c["role"] == "camop"]
        if not casters or not camops:
            await interaction.response.send_message(
                "Need at least 1 caster and 1 cam op to create the channel.", ephemeral=True
            )
            return

        channel = await create_private_match_channel(interaction, match, claims)
        if channel:
            await interaction.response.send_message(
                f"Channel created: <#{channel.id}>", ephemeral=True
            )
        else:
            await interaction.response.send_message("Failed to create channel.", ephemeral=True)

    async def _refresh_message(self, interaction: Interaction):
        """Update the message with current claims."""
        match = await db.get_match(self.match_id)
        claims = await db.get_claims(self.match_id)
        new_view = ClaimView(self.match_id, match, claims)
        try:
            await interaction.message.edit(view=new_view)
        except Exception:
            pass

    async def _handle_crew_ready(self, interaction: Interaction):
        """Send a ready message to the private channel mentioning both teams."""
        match = await db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Could not access server.", ephemeral=True)
            return

        # Check if private channel exists
        if not match.get("private_channel_id"):
            await interaction.response.send_message(
                "Private channel hasn't been created yet. Create the channel first.", ephemeral=True
            )
            return

        private_channel = guild.get_channel(match["private_channel_id"])
        if not private_channel:
            await interaction.response.send_message(
                "Private channel not found. It may have been deleted.", ephemeral=True
            )
            return

        # Find team roles
        team_a_lower = match['team_a'].lower()
        team_b_lower = match['team_b'].lower()
        team_pings = []
        for role in guild.roles:
            if role.name.lower().startswith("team:"):
                team_name = role.name[5:].strip().lower()
                if team_name == team_a_lower or team_name == team_b_lower:
                    team_pings.append(role.mention)

        # Build ready message
        if team_pings:
            pings = " ".join(team_pings)
            ready_msg = f"{pings}\n\n**The casting crew is ready!** You may start whenever you're ready."
        else:
            ready_msg = f"**{match['team_a']}** and **{match['team_b']}**\n\n**The casting crew is ready!** You may start whenever you're ready."

        await private_channel.send(ready_msg)
        await interaction.response.send_message(
            f"Ready message sent to <#{match['private_channel_id']}>!", ephemeral=True
        )

    async def _handle_go_live(self, interaction: Interaction):
        """Post a live announcement to the configured channel."""
        match = await db.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Could not access server.", ephemeral=True)
            return

        # Check if live announcement channel is configured
        if not config.LIVE_ANNOUNCEMENT_CHANNEL_ID:
            await interaction.response.send_message(
                "Live announcement channel not configured. Set LIVE_ANNOUNCEMENT_CHANNEL_ID in .env", ephemeral=True
            )
            return

        live_channel = guild.get_channel(config.LIVE_ANNOUNCEMENT_CHANNEL_ID)
        if not live_channel:
            await interaction.response.send_message(
                "Live announcement channel not found.", ephemeral=True
            )
            return

        # Find team roles
        team_a_lower = match['team_a'].lower()
        team_b_lower = match['team_b'].lower()
        team_mentions = []
        for role in guild.roles:
            if role.name.lower().startswith("team:"):
                team_name = role.name[5:].strip().lower()
                if team_name == team_a_lower:
                    team_mentions.append(role.mention)
                elif team_name == team_b_lower:
                    team_mentions.append(role.mention)

        # Use role mentions or fallback to text names
        if len(team_mentions) >= 2:
            teams_text = f"{team_mentions[0]} vs {team_mentions[1]}"
        elif len(team_mentions) == 1:
            # One team found as role
            if team_mentions[0].lower().find(team_a_lower) != -1:
                teams_text = f"{team_mentions[0]} vs {match['team_b']}"
            else:
                teams_text = f"{match['team_a']} vs {team_mentions[0]}"
        else:
            teams_text = f"{match['team_a']} vs {match['team_b']}"

        # Get live ping role
        live_ping = ""
        if config.LIVE_PING_ROLE_ID:
            live_role = guild.get_role(config.LIVE_PING_ROLE_ID)
            if live_role:
                live_ping = live_role.mention

        # Build announcement
        twitch_url = config.TWITCH_URL or "https://www.twitch.tv/echomasterleague"
        announcement = f"# [{twitch_url.split('/')[-1]}]({twitch_url}) We are live now casting {teams_text}"
        if live_ping:
            announcement += f"\n{live_ping}"

        await live_channel.send(announcement)
        await interaction.response.send_message(
            f"Live announcement posted to <#{config.LIVE_ANNOUNCEMENT_CHANNEL_ID}>!", ephemeral=True
        )


async def create_private_match_channel(
    interaction: Interaction, match: dict, claims: list[dict]
) -> discord.TextChannel | None:
    """Create a private text channel visible to casters, cam-ops, and staff."""
    guild = interaction.guild
    if guild is None:
        return None

    category = None
    if config.PRIVATE_CATEGORY_ID:
        category = guild.get_channel(config.PRIVATE_CATEGORY_ID)

    # Permission overwrites
    overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
    }

    # Add claimed users
    for c in claims:
        member = guild.get_member(c["user_id"])
        if member:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    # Add roles
    for role_id in (config.CASTER_ROLE_ID, config.CAMOP_ROLE_ID, config.STAFF_ROLE_ID):
        if role_id:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    # Add team roles (roles starting with "team:")
    team_a_lower = match['team_a'].lower()
    team_b_lower = match['team_b'].lower()
    team_roles: list[discord.Role] = []
    for role in guild.roles:
        if role.name.lower().startswith("team:"):
            team_name = role.name[5:].strip().lower()  # Get name after "team:"
            if team_name == team_a_lower or team_name == team_b_lower:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                team_roles.append(role)

    channel_name = f"{match['team_a']}-vs-{match['team_b']}".lower().replace(" ", "-")

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="CasterBot match channel",
        )
    except Exception:
        return None

    await db.set_private_channel(match["match_id"], channel.id)

    # Post rosters / match info with team pings
    roster_msg = _build_roster_message(match, claims, team_roles)
    await channel.send(roster_msg)
    
    # Post close channel button
    close_view = CloseChannelView(match["match_id"])
    await channel.send(view=close_view)
    
    return channel


def _build_roster_message(match: dict, claims: list[dict], team_roles: list[discord.Role] | None = None) -> str:
    # Ping team roles first
    pings = ""
    if team_roles:
        pings = " ".join(role.mention for role in team_roles)
    
    lines = [
        f"# `{match['team_a']}` ⚔️ `{match['team_b']}`",
        f"**When:** {_format_match_time(match)}",
        "",
    ]
    
    if pings:
        lines.append(pings)
        lines.append("")
    
    lines.append("## 🎥 YOU ARE BEING CASTED 🎥")
    lines.append("This match is live for coverage. Bring the energy.")
    lines.append("**DO NOT START** until the casters give the official OK.")
    lines.append("Wait for confirmation in chat.")
    lines.append("Let us know if you need a server.")
    lines.append("")
    
    lines.append("**Staff:**")
    for c in claims:
        role_label = c["role"].title()
        if c["role"] == "caster":
            role_label = f"Caster {c['slot']}"
        lines.append(f"- {role_label}: <@{c['user_id']}>")

    return "\n".join(lines)


class CloseChannelView(LayoutView):
    """View with a 2-step close channel button for casters only."""

    def __init__(self, match_id: str, confirming: bool = False):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.confirming = confirming
        
        if confirming:
            # Show confirm button
            confirm_btn = Button(
                style=ButtonStyle.danger,
                label="⚠️ CONFIRM DELETE",
                custom_id=f"confirm_close:{match_id}",
            )
            cancel_btn = Button(
                style=ButtonStyle.secondary,
                label="Cancel",
                custom_id=f"cancel_close:{match_id}",
            )
            container = Container(
                TextDisplay("**Are you sure you want to delete this channel?**\nThis cannot be undone."),
                ActionRow(confirm_btn, cancel_btn),
                accent_color=discord.Color.red(),
            )
        else:
            # Show initial close button
            close_btn = Button(
                style=ButtonStyle.danger,
                label="🔒 Close Channel",
                custom_id=f"close_channel:{match_id}",
            )
            container = Container(
                ActionRow(close_btn),
            )
        
        self.add_item(container)

    async def interaction_check(self, interaction: Interaction) -> bool:
        custom_id = interaction.data.get("custom_id", "")
        
        # Check if user has caster or staff role
        if not self._can_close_channel(interaction):
            await interaction.response.send_message(
                "Only casters or staff can close the channel.", ephemeral=True
            )
            return False
        
        if custom_id.startswith("close_channel:"):
            # Show confirmation
            confirm_view = CloseChannelView(self.match_id, confirming=True)
            await interaction.response.edit_message(view=confirm_view)
            return False
        elif custom_id.startswith("confirm_close:"):
            # Actually delete the channel
            await interaction.response.send_message("Closing channel in 3 seconds...", ephemeral=True)
            
            # Get match info for transcript
            match = await db.get_match(self.match_id)
            
            # Get claims BEFORE deleting match (for leaderboard)
            claims = await db.get_claims(self.match_id)
            
            # Increment cast count for all casters and cam ops
            for claim in claims:
                if claim["role"] in ("caster", "camop"):
                    await db.increment_cast_count(claim["user_id"])
            
            # Generate and post transcript if configured
            if config.TRANSCRIPT_CHANNEL_ID and interaction.channel:
                await self._create_transcript(interaction, match)
            
            # Delete claim message from claim channel if it exists
            if match and match.get("message_id"):
                try:
                    claim_channel = interaction.client.get_channel(config.CLAIM_CHANNEL_ID)
                    if claim_channel:
                        msg = await claim_channel.fetch_message(match["message_id"])
                        await msg.delete()
                except discord.NotFound:
                    pass
                except Exception:
                    pass
            
            # Delete match from DB (this also clears claims)
            await db.delete_match(self.match_id)
            await asyncio.sleep(3)
            try:
                await interaction.channel.delete(reason="Match channel closed by caster")
            except Exception:
                pass
            return False
        elif custom_id.startswith("cancel_close:"):
            # Go back to normal view
            normal_view = CloseChannelView(self.match_id, confirming=False)
            await interaction.response.edit_message(view=normal_view)
            return False
        
        return True

    def _can_close_channel(self, interaction: Interaction) -> bool:
        """Check if user has the caster or staff role."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        
        # Allow if no roles configured
        if not config.CASTER_ROLE_ID and not config.STAFF_ROLE_ID:
            return True
        
        # Check for caster or staff role
        allowed_role_ids = []
        if config.CASTER_ROLE_ID:
            allowed_role_ids.append(config.CASTER_ROLE_ID)
        if config.STAFF_ROLE_ID:
            allowed_role_ids.append(config.STAFF_ROLE_ID)
        
        return any(r.id in allowed_role_ids for r in member.roles)

    async def _create_transcript(self, interaction: Interaction, match: dict | None) -> None:
        """Create and post a transcript of the channel to the transcript channel."""
        transcript_channel = interaction.client.get_channel(config.TRANSCRIPT_CHANNEL_ID)
        if not transcript_channel:
            return
        
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return
        
        # Collect messages
        messages = []
        try:
            async for msg in channel.history(limit=500, oldest_first=True):
                messages.append(msg)
        except Exception:
            return
        
        if not messages:
            return
        
        # Get season/week settings
        season = await db.get_setting("season") or ""
        week = await db.get_setting("week") or ""
        
        # Build transcript text
        transcript = StringIO()
        
        # Header
        if match:
            transcript.write(f"TRANSCRIPT: {match['team_a']} vs {match['team_b']}\n")
            # Season and week from settings
            if season:
                transcript.write(f"Season: {season}\n")
            if week:
                transcript.write(f"Week: {week}\n")
            # Match type (Assigned/Challenge) from match data
            match_type = match.get('match_type', '')
            if match_type:
                transcript.write(f"Match Type: {match_type}\n")
            # Match date/time in Eastern
            ts = match.get('match_timestamp')
            if ts:
                eastern = dateutil_tz.gettz('US/Eastern')
                match_dt = datetime.fromtimestamp(ts, tz=eastern)
                transcript.write(f"Match Date/Time: {match_dt.strftime('%B %d, %Y at %I:%M %p')} ET\n")
            else:
                transcript.write(f"Match Date: {match.get('match_date', 'Unknown')} {match.get('match_time', '')}\n")
        else:
            transcript.write(f"TRANSCRIPT: {channel.name}\n")
        
        transcript.write(f"Channel: #{channel.name}\n")
        transcript.write(f"Closed by: {interaction.user.display_name} ({interaction.user.id})\n")
        transcript.write(f"Closed at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
        transcript.write(f"Message count: {len(messages)}\n")
        transcript.write("=" * 60 + "\n\n")
        
        # Messages
        for msg in messages:
            timestamp = msg.created_at.strftime('%Y-%m-%d %H:%M:%S')
            author = f"{msg.author.display_name} ({msg.author.name})"
            transcript.write(f"[{timestamp}] {author}:\n")
            
            if msg.content:
                transcript.write(f"{msg.content}\n")
            
            # Note attachments
            for attachment in msg.attachments:
                transcript.write(f"[Attachment: {attachment.filename} - {attachment.url}]\n")
            
            # Note embeds
            if msg.embeds:
                transcript.write(f"[{len(msg.embeds)} embed(s)]\n")
            
            transcript.write("\n")
        
        # Create file and send
        transcript_content = transcript.getvalue()
        transcript.close()
        
        # Build summary embed
        embed = discord.Embed(
            title="Channel Transcript",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        if match:
            embed.add_field(name="Match", value=f"{match['team_a']} vs {match['team_b']}", inline=True)
            if season:
                embed.add_field(name="Season", value=season, inline=True)
            if week:
                embed.add_field(name="Week", value=week, inline=True)
            match_type = match.get('match_type', '')
            if match_type:
                embed.add_field(name="Type", value=match_type, inline=True)
            ts = match.get('match_timestamp')
            if ts:
                eastern = dateutil_tz.gettz('US/Eastern')
                match_dt = datetime.fromtimestamp(ts, tz=eastern)
                embed.add_field(name="Match Time", value=f"{match_dt.strftime('%b %d, %Y %I:%M %p')} ET", inline=True)
        embed.add_field(name="Channel", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Messages", value=str(len(messages)), inline=True)
        
        # Send as file attachment
        filename = f"transcript-{channel.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.txt"
        file = discord.File(StringIO(transcript_content), filename=filename)
        
        try:
            await transcript_channel.send(embed=embed, file=file)
        except Exception:
            pass
