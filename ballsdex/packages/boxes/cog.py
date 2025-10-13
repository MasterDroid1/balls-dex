import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, timezone
import random
from discord import Embed, Color, File
from tortoise import models, fields
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
from discord.ui import View
import asyncio
import logging
logger = logging.getLogger(__name__)
from ballsdex.core.utils.transformers import (
    BallTransform,
    SpecialTransform,
)
from ballsdex.core.models import (
    Ball,
    balls,
    BallInstance,
    BlacklistedGuild,
    BlacklistedID,
    GuildConfig,
    Player,
    Trade,
    TradeObject,
    Special,
)
from ballsdex.settings import settings
from ballsdex.core.bot import BallsDexBot
import ballsdex.packages.config.components as Components
from collections import defaultdict
from ballsdex.core.image_generator. image_gen import draw_card
from io import BytesIO
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)

# Credits
# -------
# - crashtestalex
# - hippopotis
# - dot_zz
# -------

# Track last claim times
last_daily_times = {}
last_weekly_times = {}
wallet_balance = defaultdict(int)
packly_pool = defaultdict(int)

# Custom daily usage tracking - stores {user_id: {'count': int, 'first_use': datetime}}
daily_usage_tracking = {}

# Owners who can give packs
ownersid = {
    749658746535280771,
    1184739489315299339,
    1079166030166896711      
}

# Cooldowns
DAILY_COOLDOWN = timedelta(hours=24)
WEEKLY_COOLDOWN = timedelta(days=7)
gamble_cooldowns = {} 


class SkipView(View):
    """View for skip button during multipack opening"""
    def __init__(self):
        super().__init__(timeout=60)
        self.skipped = False

    @discord.ui.button(label="Skip Animation", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.skipped = True
        button.label = "Skipped ✓"
        button.disabled = True
        button.style = discord.ButtonStyle.success
        
        # Update embed to show skip confirmation
        current_embed = interaction.message.embeds[0]
        current_embed.description = "⏩ **Animation skipped! Showing final results...**"
        current_embed.color = Color.green()
        
        await interaction.response.edit_message(embed=current_embed, view=self)
        self.stop()


class Claim(commands.GroupCog, name="packs"):
    """
    A little simple daily pack!
    """

    def __init__(self, bot: BallsDexBot):
        self.bot = bot
        self.bot_tutorial_seen = set()
        self.bot_walletturorial_seen = set()
        super().__init__()

    async def get_random_special(self) -> Special | None:
        """
        Get a random special based on rarity probability and date restrictions.
        Returns None if no special is selected or available.
        """
        now = datetime.now(timezone.utc)
        
        # Get all active specials that respect date restrictions
        try:
            from tortoise.expressions import Q
            active_specials = await Special.filter(
                # Check start_date and end_date constraints
                Q(start_date__isnull=True) | Q(start_date__lte=now),
                Q(end_date__isnull=True) | Q(end_date__gte=now),
                # Only include specials that are not hidden
                hidden=False
            ).all()
        except:
            # Fallback if Q import fails
            active_specials = await Special.all()
        
        if not active_specials:
            return None
        
        # Apply rarity probability for each special
        for special in active_specials:
            if random.random() < special.rarity:
                return special
        
        return None

    async def get_random_ball(self, player: Player) -> Ball | None:
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=30.0, enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            # base weight based on ownership
            base_weight = 1 if ball.id in owned_ids else 1

            # rarity weight according to your tiers
            if 5.0 <= ball.rarity <= 30.0:
                rarity_weight = 1600  # common
            elif 2.5 <= ball.rarity < 5.0:
                rarity_weight = 600   # decent
            elif 1.5 <= ball.rarity < 2.5:
                rarity_weight = 300  # rare
            elif 0.5 < ball.rarity < 1.5:
                rarity_weight = 100  # very rare
            elif 0.1 < ball.rarity < 0.5:
                rarity_weight = 30 # very very rare 
            elif 0.03 <= ball.rarity <= 0.1:
                rarity_weight = 20  # ultra rare

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return None

        return random.choice(choices)

    def check_daily_usage(self, user_id: str) -> tuple[bool, int]:
        """
        Check if user can use daily command and return remaining uses.
        Returns (can_use, remaining_uses)
        """
        now = datetime.now(timezone.utc)
        
        if user_id not in daily_usage_tracking:
            # First time using daily command
            daily_usage_tracking[user_id] = {
                'count': 0,
                'first_use': now
            }
            return True, 3
        
        user_data = daily_usage_tracking[user_id]
        time_since_first_use = now - user_data['first_use']
        
        # Reset if 24 hours have passed since first use
        if time_since_first_use >= DAILY_COOLDOWN:
            daily_usage_tracking[user_id] = {
                'count': 0,
                'first_use': now
            }
            return True, 3
        
        # Check if user has used all 3 attempts
        if user_data['count'] >= 3:
            return False, 0
        
        remaining = 3 - user_data['count']
        return True, remaining

    def increment_daily_usage(self, user_id: str):
        """Increment the daily usage count for a user"""
        if user_id in daily_usage_tracking:
            daily_usage_tracking[user_id]['count'] += 1

    def get_daily_cooldown_remaining(self, user_id: str) -> timedelta | None:
        """Get remaining cooldown time for daily command"""
        if user_id not in daily_usage_tracking:
            return None
        
        user_data = daily_usage_tracking[user_id]
        if user_data['count'] < 3:
            return None
        
        now = datetime.now(timezone.utc)
        cooldown_end = user_data['first_use'] + DAILY_COOLDOWN
        
        if now >= cooldown_end:
            return None
        
        return cooldown_end - now

    async def getdasigmaballmate(self, player: Player) -> Ball | None:
        owned_ids = set(
            await BallInstance.filter(player=player).values_list("ball__id", flat=True)
        )
        all_balls = await Ball.filter(rarity__gte=0.03, rarity__lte=5.0, enabled=True).all()

        if not all_balls:
            return None

        weighted_choices = []
        for ball in all_balls:
            if ball.id in owned_ids:
                base_weight = 1
            else:
                base_weight = 1

            # Explicit rarity weighting
            if ball.rarity >= 4.5:  # very common
                rarity_weight = 900
            elif ball.rarity >= 1.5:  # common
                rarity_weight = 500
            elif ball.rarity >= 0.5:  # uncommon
                rarity_weight = 200
            else:  # rare (below 0.5 rarity)
                rarity_weight = 20

            final_weight = base_weight * rarity_weight
            weighted_choices.append((ball, final_weight))

        choices = []
        for ball, weight in weighted_choices:
            choices.extend([ball] * int(weight))

        if not choices:
            return None

        return random.choice(choices)

    def format_special_emoji(self, special: Special | None) -> str:
        """Format special emoji for display"""
        if not special:
            return ""
        
        if special.emoji:
            try:
                emoji_id = int(special.emoji)
                emoji = self.bot.get_emoji(emoji_id) or "⚡"
                return str(emoji)
            except ValueError:
                return special.emoji
        return "⚡"

    @app_commands.command(name="daily", description="Claim your daily Footballer! (3 uses per day)")
    async def daily(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Check account age requirement
        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        # Check daily usage limits
        can_use, remaining_uses = self.check_daily_usage(user_id)
        
        if not can_use:
            cooldown_remaining = self.get_daily_cooldown_remaining(user_id)
            if cooldown_remaining:
                hours = int(cooldown_remaining.total_seconds() // 3600)
                minutes = int((cooldown_remaining.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"⏰ You've used all 3 daily packs! Come back in {hours}h {minutes}m for your next set of daily packs.",
                    ephemeral=True
                )
                return

        await interaction.response.defer()
        
        # Increment usage count
        self.increment_daily_usage(user_id)
        
        # Get updated remaining uses after incrementing
        _, new_remaining = self.check_daily_usage(user_id)
        player, _ = await Player.get_or_create(discord_id=str(user_id))
        ball = await self.get_random_ball(player)

        if not ball:
            await interaction.followup.send("No balls are available.", ephemeral=True)
            return

        # Get random special for this pack
        special = await self.get_random_special()

        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        # Walkout starts here
        walkout_embed = Embed(title="🎉 Daily Pack Opening...", color=Color.dark_gray())
        remaining_text = f"Remaining daily uses: {new_remaining}/3" if new_remaining > 0 else "All daily uses consumed! Come back tomorrow."
        walkout_embed.set_footer(text=remaining_text)
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        # Add special information to walkout if special exists
        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"
            
            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎁 You got **{ball.country}**{special_text}"
        walkout_embed.color = Color.gold()

        # Generate image card
        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

        # ✅ Log it
        log_channel_id = 1361522228021297404  # <- Replace with your logging channel ID
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        special_info = f" | Special: {special.name}" if special else ""

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Daily pack and got **{ball.country}** (Use {3-new_remaining}/3){special_info}\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"• Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[DAILY PACK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Daily use {3-new_remaining}/3 | Footballer ID: `#{ball.pk:0X}`{special_info}"
        )


    @app_commands.command(name="weekly", description="Claim your weekly Footballer!")
    @app_commands.checks.cooldown(1, 604800, key=lambda i: i.user.id)
    async def weekly(self, interaction: discord.Interaction[BallsDexBot]):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        now = datetime.now()
        last_claim = last_weekly_times.get(user_id)


        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        ball = await self.getdasigmaballmate(player)

        if not ball:
            await interaction.response.send_message("No balls are available.", ephemeral=True)
            return

        # Get random special for this pack
        special = await self.get_random_special()

        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        # Walkout-style embed animation
        walkout_embed = discord.Embed(title="🎉 Weekly Pack Opening...", color=discord.Color.dark_gray())
        walkout_embed.set_footer(text="Come back in 7 days for your next claim!")
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        # Add special information to walkout if special exists
        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"
            
            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎁 You got **{ball.country}**{special_text}"
        walkout_embed.color = discord.Color.from_rgb(229, 255, 0)  # You can randomize if you want

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()


        # ✅ Log the weekly pack grant to a specific channel and the bot's logger
        log_channel_id = 1361522228021297404  # <- Replace with your logging channel ID
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        special_info = f" | Special: {special.name}" if special else ""

        if log_channel:
            await log_channel.send(
                f"**{interaction.user.mention}** claimed a Weekly pack and got **{ball.country}**{special_info}\n"
                f"• Rarity: `{ball.rarity}` 💖 `{instance.attack_bonus}` ⚽ `{instance.health_bonus}`\n"
                f"Footballer ID: `#{ball.pk:0X}`\n"
                f"• Account created: `{account_created}`"
            )

        logger.info(
            f"[WEEKLY PACK] {interaction.user} ({interaction.user.id}) received {ball.country} "
            f"(Rarity: {ball.rarity}) | Account created: {account_created} | "
            f"Footballer ID: `#{ball.pk:0X}`{special_info}"
        )





    # Main /packly command to claim a ball after using a pack
    @app_commands.command(name="packly", description="Claim your footballer from the packly!")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def packly(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return
        
        # Ensure user starts with 1 pack if no balance is set
        if user_id not in wallet_balance:
            wallet_balance[user_id] = 1  # Initialize with 1 pack

        # Check if the user has enough packs to claim
        if wallet_balance[user_id] < 1:
            await interaction.response.send_message(
                "You don't have enough packs!",
                ephemeral=True
            )
            return

        # Deduct 1 pack from user's wallet for claiming a ball
        wallet_balance[user_id] -= 1

        # Assign a random ball to the user
        player, _ = await Player.get_or_create(discord_id=str(interaction.user.id))
        ball = await self.get_random_ball(player)

        if not ball:
            await interaction.response.send_message("No footballers are available.", ephemeral=True)
            return

        # Get random special for this pack
        special = await self.get_random_special()

        # Create an instance of the ball for the user
        instance = await BallInstance.create(
            ball=ball,
            player=player,
            attack_bonus=random.randint(-20, 20),
            health_bonus=random.randint(-20, 20),
            special=special,
        )

        # Walkout-style embed animation
        walkout_embed = discord.Embed(title="🎁 Opening Packly...", color=discord.Color.dark_gray())
        walkout_embed.set_footer(text="FootballDex Packly")
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=walkout_embed)


        await asyncio.sleep(1.5)
        walkout_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
        walkout_embed.description += f"\n💳 **Card:** **{regime_name}**"
        await msg.edit(embed=walkout_embed)

        # Add special information to walkout if special exists
        if special:
            await asyncio.sleep(1.5)
            special_emoji = ""
            if special.emoji:
                try:
                    emoji_id = int(special.emoji)
                    special_emoji = self.bot.get_emoji(emoji_id) or "⚡"
                except ValueError:
                    special_emoji = special.emoji
            else:
                special_emoji = "⚡"
            
            walkout_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
            await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        walkout_embed.description += f"\n💖 **Health:** `{instance.health}`\n⚽ **Attack:** `{instance.attack}`"
        await msg.edit(embed=walkout_embed)

        await asyncio.sleep(1.5)
        special_text = f" with **{special.name}** special!" if special else "!"
        walkout_embed.title = f"🎉 You claimed **{ball.country}** from Packly{special_text}"
        walkout_embed.color = discord.Color.gold()

        content, file, view = await instance.prepare_for_message(interaction)
        walkout_embed.set_image(url="attachment://" + file.filename)
        walkout_embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await msg.edit(embed=walkout_embed, attachments=[file], view=view)
        file.close()

    @app_commands.command(name="multipackly", description="Open multiple packs at once using your pack balance!")
    @app_commands.describe(amount="How many packs to open (1-100)")
    async def multipackly(self, interaction: discord.Interaction[BallsDexBot], amount: int = 1):
        user_id = str(interaction.user.id)
        
        # Validate amount
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                "❌ You can only open between 1 and 100 packs at once!",
                ephemeral=True
            )
            return

        # Check account age requirement
        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        # Check if user has enough packs
        current_balance = wallet_balance.get(user_id, 0)
        if current_balance < amount:
            await interaction.response.send_message(
                f"❌ You don't have enough packs! You have {current_balance} packs, but need {amount}.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        
        # Deduct packs from balance
        wallet_balance[user_id] -= amount
        
        player, _ = await Player.get_or_create(discord_id=str(user_id))
        
        # Store all opened instances and their data
        opened_instances = []
        pack_data = []
        
        # Create skip view
        skip_view = SkipView()
        
        # Opening animation embed
        opening_embed = Embed(
            title=f"🎉 Opening {amount} Packs...",
            description="⏭️ **Click the Skip Animation button below to see all results instantly!**\n\n*Opening packs one by one...*",
            color=Color.blue()
        )
        opening_embed.set_footer(text=f"Remaining pack balance: {wallet_balance.get(user_id, 0)}")
        
        msg = await interaction.followup.send(embed=opening_embed, view=skip_view)
        
        # Generate all packs first
        for i in range(amount):
            ball = await self.get_random_ball(player)
            if not ball:
                continue
                
            special = await self.get_random_special()
            
            instance = await BallInstance.create(
                ball=ball,
                player=player,
                attack_bonus=random.randint(-20, 20),
                health_bonus=random.randint(-20, 20),
                special=special,
            )
            
            opened_instances.append(instance)
            pack_data.append({
                'ball': ball,
                'instance': instance,
                'special': special,
                'pack_number': i + 1
            })
        
        # Animation phase (can be skipped)
        if not skip_view.skipped and pack_data:
            for i, data in enumerate(pack_data):
                if skip_view.skipped:
                    break
                    
                ball = data['ball']
                special = data['special']
                pack_num = data['pack_number']
                
                # Update embed for current pack
                pack_embed = Embed(
                    title=f"🎁 Pack {pack_num}/{amount}",
                    color=Color.blue()
                )
                
                # Show pack opening animation
                pack_embed.description = "Opening pack..."
                await msg.edit(embed=pack_embed, view=skip_view)
                await asyncio.sleep(0.8)
                
                if skip_view.skipped:
                    break
                
                pack_embed.description = f"✨ **Rarity:** `{ball.rarity}`"
                await msg.edit(embed=pack_embed, view=skip_view)
                await asyncio.sleep(0.8)
                
                if skip_view.skipped:
                    break
                
                regime_name = ball.cached_regime.name if ball.cached_regime else "Unknown"
                pack_embed.description += f"\n💳 **Card:** **{regime_name}**"
                await msg.edit(embed=pack_embed, view=skip_view)
                await asyncio.sleep(0.8)
                
                if skip_view.skipped:
                    break
                
                # Add special info if exists
                if special:
                    special_emoji = self.format_special_emoji(special)
                    pack_embed.description += f"\n{special_emoji} **Special:** **{special.name}**"
                    await msg.edit(embed=pack_embed, view=skip_view)
                    await asyncio.sleep(0.8)
                
                if skip_view.skipped:
                    break
                
                special_text = f" with **{special.name}** special!" if special else "!"
                pack_embed.title = f"🎁 Pack {pack_num}/{amount}: **{ball.country}**{special_text}"
                pack_embed.color = Color.green()
                await msg.edit(embed=pack_embed, view=skip_view)
                
                # Short pause before next pack
                if i < len(pack_data) - 1:  # Don't pause after last pack
                    await asyncio.sleep(1.2)
                    if skip_view.skipped:
                        break
        
        # Final results summary - simplified format (only ball names and specials)
        if pack_data:
            results_embed = Embed(
                title=f"📦 Multi-Pack Results ({amount} packs opened)",
                color=Color.gold()
            )
            
            # Create simplified results list
            results_list = []
            for i, data in enumerate(pack_data, 1):
                ball = data['ball']
                special = data['special']
                
                # Only show ball name and special emoji if it exists
                if special:
                    special_emoji = self.format_special_emoji(special)
                    line = f"`{i}.` **{ball.country}** {special_emoji}"
                else:
                    line = f"`{i}.` **{ball.country}**"
                
                results_list.append(line)
            
            # Split results into chunks if too long
            results_text = "\n".join(results_list)
            if len(results_text) > 4000:  # Discord embed limit is 4096
                # Split into multiple embeds if needed
                chunk_size = 3800
                chunks = [results_text[i:i+chunk_size] for i in range(0, len(results_text), chunk_size)]
                results_embed.description = chunks[0]
                if len(chunks) > 1:
                    results_embed.set_footer(text=f"Showing results 1/{len(chunks)} - More results below")
            else:
                results_embed.description = results_text
            
            results_embed.set_footer(text=f"Pack balance remaining: {wallet_balance.get(user_id, 0)}")
            results_embed.set_author(
                name=interaction.user.display_name, 
                icon_url=interaction.user.display_avatar.url
            )
            
            # Disable skip button and update message
            skip_view.stop()
            for item in skip_view.children:
                if hasattr(item, 'disabled'):
                    item.disabled = True
                    if hasattr(item, 'label'):
                        item.label = "Results Shown ✓"
                        item.style = discord.ButtonStyle.success
            
            await msg.edit(embed=results_embed, view=skip_view)
            
            # Send additional embeds if results were too long
            if len(results_text) > 4000:
                chunk_size = 3800
                chunks = [results_text[i:i+chunk_size] for i in range(0, len(results_text), chunk_size)]
                for i, chunk in enumerate(chunks[1:], 2):
                    extra_embed = Embed(
                        title=f"📦 Multi-Pack Results (continued {i}/{len(chunks)})",
                        description=chunk,
                        color=Color.gold()
                    )
                    await interaction.followup.send(embed=extra_embed)
        
        # Log the multi-pack opening
        log_channel_id = 1361522228021297404
        log_channel = self.bot.get_channel(log_channel_id)
        account_created = interaction.user.created_at.strftime("%Y-%m-%d %H:%M:%S")
        
        if log_channel and pack_data:
            # Create log summary
            special_count = sum(1 for data in pack_data if data['special'])
            total_balls = len(pack_data)
            
            log_embed = Embed(
                title=f"Multi-Pack Opening Log",
                color=Color.blue()
            )
            log_embed.add_field(
                name="User",
                value=f"{interaction.user.mention} ({interaction.user.id})",
                inline=False
            )
            log_embed.add_field(
                name="Summary",
                value=f"Opened {total_balls} packs, {special_count} with specials",
                inline=False
            )
            
            # Add details for each ball
            details = []
            for data in pack_data:
                ball = data['ball']
                special = data['special']
                special_info = f" | {special.name}" if special else ""
                details.append(f"• {ball.country} (R:{ball.rarity}){special_info}")
            
            details_text = "\n".join(details)
            if len(details_text) > 1000:
                details_text = details_text[:997] + "..."
            
            log_embed.add_field(
                name="Balls Received",
                value=details_text,
                inline=False
            )
            log_embed.add_field(
                name="Account Created",
                value=account_created,
                inline=True
            )
            
            await log_channel.send(embed=log_embed)

        logger.info(
            f"[MULTI-PACK] {interaction.user} ({interaction.user.id}) opened {amount} packs, "
            f"got {len(pack_data)} balls with {sum(1 for data in pack_data if data['special'])} specials"
        )


    
    # Command to add packs to a user's wallet
    @app_commands.command(name="owners-add", description="Add packs to another user's wallet")
    async def ownerspacklyadd(self, interaction: discord.Interaction, user: discord.User, packs: int):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Check if the user issuing the command is allowed to add packs
        if interaction.user.id not in ownersid:
            await interaction.response.send_message(
                "You are not allowed to add packly's to other people or youself ❌",
                ephemeral=True
            )
            return

        # Ensure the target user has a wallet entry
        target_user_id = str(user.id)
        if target_user_id not in wallet_balance:
            wallet_balance[target_user_id] = 1  # Initialize with 1 pack if no balance exists

        # Add packs to the target user's wallet
        wallet_balance[target_user_id] += packs

        embed = discord.Embed(
            title="FootballDex Packs Added!",
            description=(
                f"{interaction.user.mention} has added **{packs}** pack(s) to {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{wallet_balance[target_user_id]} packs`"
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text="Packly System")
        embed.set_thumbnail(url=user.display_avatar.url)

        await interaction.response.send_message(embed=embed)
        
            # Command to remove packs from a user's wallet
    @app_commands.command(name="owners-remove", description="Remove packs from another user's wallet")
    async def ownerspacklyremove(self, interaction: discord.Interaction, user: discord.User, packs: int):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Check if the user issuing the command is allowed to remove packs
        if interaction.user.id not in ownersid:
            await interaction.response.send_message(
                "You are not allowed to remove packly's from other people or youself ❌",
                ephemeral=True
            )
            return

        # Ensure the target user has a wallet entry
        target_user_id = str(user.id)
        if target_user_id not in wallet_balance:
            wallet_balance[target_user_id] = 0  # Initialize with 0 packs if no balance exists

        # Remove packs from the target user's wallet (ensure it doesn't go below 0)
        wallet_balance[target_user_id] = max(0, wallet_balance[target_user_id] - packs)

        embed = discord.Embed(
            title="FootballDex Packs Removed!",
            description=(
                f"{interaction.user.mention} has removed **{packs}** pack(s) from {user.mention}'s wallet.\n"
                f"🪙 **{user.name}'s New Balance**: `{wallet_balance[target_user_id]} packs`"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text="Packly System")
        embed.set_thumbnail(url=user.display_avatar.url)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="gamblepack", description="Gamble your packlys for a chance to win double – or lose it all!")
    @app_commands.describe(amount="How many packs to gamble (fixed 50/50 chance)")
    async def gamblepack(self, interaction: discord.Interaction, amount: int = 1):
        user_id = str(interaction.user.id)

        min_creation = datetime.now(timezone.utc) - timedelta(days=14)
        if interaction.user.created_at > min_creation:
            await interaction.response.send_message(
                "Your account must be at least 14 days old to use this command.",
                ephemeral=True
            )
            return

        now = datetime.utcnow()

        if amount < 1:
            await interaction.response.send_message("You must gamble at least 1 pack.", ephemeral=True)
            return

        if amount > 100:
            await interaction.response.send_message("❌ You can only gamble up to 100 packlys at once.", ephemeral=True)
            return


        # Ensure user has balance
        if user_id not in wallet_balance:
            wallet_balance[user_id] = 0

        if wallet_balance[user_id] < amount:
            await interaction.response.send_message("❌ You don't have enough packlys to gamble that many.", ephemeral=True)
            return

        # Deduct packs immediately
        wallet_balance[user_id] -= amount

        await interaction.response.defer()

        suspense = discord.Embed(
            title=f"🎲 Gambling {amount} packly{'s' if amount > 1 else ''}...",
            description="Rolling the dice...",
            color=discord.Color.dark_grey()
        )
        suspense.set_footer(text="Good luck...")
        msg = await interaction.followup.send(embed=suspense)

        await asyncio.sleep(2)

        # Always 50/50 win chance
        result = "win" if random.choice([True, False]) else "lose"

        if result == "win":
            reward = amount * 2
            wallet_balance[user_id] += reward
            suspense.title = f"🎉 You WON {reward} packlys!"
            suspense.color = discord.Color.green()
            suspense.description = f"Luck is on your side. You risked {amount}, and won {reward}!"
        else:
            suspense.title = f"💀 You LOST your {amount} packly{'s' if amount > 1 else ''}!"
            suspense.color = discord.Color.red()
            suspense.description = "Bad luck... you lost it all."

        await msg.edit(embed=suspense)

        # Optional log
        log_channel_id = 1341228457417248940
        log_channel = self.bot.get_channel(log_channel_id)
        if log_channel:
            await log_channel.send(
                f"🎲 **{interaction.user.mention}** gambled `{amount}` packlys and **{result.upper()}**.\n"
                f"🎯 Win chance: `50%`\n"
                f"📦 New balance: `{wallet_balance[user_id]}`"
            )

    
    # Command to check wallet balance
    @app_commands.command(name="wallet", description="Check your wallet balance")
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def wallet(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Show tutorial once per user
        if user_id not in self.bot_walletturorial_seen:
            tutorial_embed = discord.Embed(
                title="Welcome To The Packlys Wallet Command!",
                description=(
                    "Use `/packs wallet` to check your packlys balance.\n"
                    "- You start with 0 Packlys.\n"
                    "- To get more packlys, you have to ask the owners of FootballDex to add them!\n"
                    "- Join **[FootballDex](https://discord.gg/footballdex) to get free packlys!**\n"
                    "- These packlys can be used for `/packs packlys` `/packs multipackly` and `/packs gamblepack`\n"
                    "Enjoy!"
                ),
                color=discord.Color.gold()
            )
            await interaction.response.send_message(embed=tutorial_embed, ephemeral=True)
            self.bot_walletturorial_seen.add(user_id)
            return  # Stop here, so user reads tutorial first
        
        # Get the user's pack balance (defaults to 0 if they haven't added any packs)
        balance = wallet_balance.get(user_id, 0)
        
        embed = discord.Embed(
            title=f"{username}'s Wallet",
            description=f"You currently have **{balance}** packly(s).",
            color=discord.Color.green()
        )
        embed.set_footer(text="FootballDex Wallet")
        
        # Send the wallet balance as an embed
        await interaction.response.send_message(embed=embed, ephemeral=False)
