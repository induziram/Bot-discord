
# k1LL_bot.py
# Bot Discord completo (~700+ linhas) com sistema de tickets CORRIGIDO e persistente.
# Compatível com discord.py 2.x
#
# Como usar:
#  - pip install -r requirements.txt
#  - Configure o .env com DISCORD_TOKEN=seu_token
#  - python main.py
#
# Principais features:
#  • /setup (logs, boas-vindas, autorole, categoria de tickets, anti-links)
#  • Tickets: /ticketpanel, botão Abrir Ticket (persistente), /ticketclose, /ticketadd, /ticketremove
#  • Moderação: /ban, /unban, /kick, /mute, /unmute, /clear, /slowmode, /warn, /warnings, /clearwarns
#  • XP/Rank: /rank, /leaderboard
#  • Economia: /balance, /daily, /pay, /shop, /buy, /inventory
#  • Util: /ping, /serverinfo, /userinfo, /suggest, /poll
#
# Nota sobre tickets (o problema mais comum):
#  - É necessário rodar /setup e garantir que o bot tenha "Manage Channels" e "Manage Roles".
#  - A View dos botões é PERSISTENTE: ela fica registrada no setup_hook com custom_ids fixos.
#  - Previne ticket duplicado por usuário; transcreve e marca como fechado corretamente.

from __future__ import annotations

import os
import re
import io
import json
import time
import math
import sqlite3
from typing import Optional, Iterable
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

# ---------- ENV ----------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("BOT_DB", "k1ll.db")
DEFAULT_PREFIX = os.getenv("BOT_PREFIX", "/")
BRAND = "k1LL"

# ---------- INTENTS ----------
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.messages = True
INTENTS.reactions = True

# ---------- CONSTANTES ----------
ANTI_LINK_REGEX = re.compile(r"(?:https?://|discord\.gg/|www\.)", re.I)
MSG_XP_COOLDOWN = 3.0
SPAM_WINDOW = 8.0
SPAM_LIMIT = 7
MENTION_LIMIT = 8
DAILY_AMOUNT = 250
SHOP_ITEMS = {"vip": 1500, "nickcolor": 800, "crate": 400}

# ===================== DB ===================== #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER,
            welcome_channel_id INTEGER,
            autorole_id INTEGER,
            ticket_category_id INTEGER,
            anti_links INTEGER DEFAULT 0
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS warns (
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            timestamp TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS xp (
            guild_id INTEGER,
            user_id INTEGER,
            xp INTEGER,
            level INTEGER,
            last_msg_ts REAL,
            PRIMARY KEY (guild_id, user_id)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS economy (
            guild_id INTEGER,
            user_id INTEGER,
            balance INTEGER,
            last_daily TEXT,
            inv_json TEXT,
            PRIMARY KEY (guild_id, user_id)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            guild_id INTEGER,
            user_id INTEGER,
            channel_id INTEGER,
            open INTEGER,
            created_at TEXT
        )""")
        conn.commit()

# ===================== BOT CORE ===================== #
class K1LLBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(DEFAULT_PREFIX),
            intents=INTENTS
        )
        self.synced = False
        self._spam_cache: dict[int, list[float]] = {}

    async def setup_hook(self) -> None:
        init_db()
        # Views persistentes: IMPORTANTÍSSIMO ter os mesmos custom_ids
        self.add_view(TicketPanelView())
        self.add_view(RoleMenuPersist())

    async def on_ready(self):
        if not self.synced:
            await self.tree.sync()
            self.synced = True
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name=f"{BRAND} | use /help")
        )
        print(f"✅ {BRAND} online como {self.user} em {len(self.guilds)} servidores.")

bot = K1LLBot()

# ===================== UTIL ===================== #
async def get_cfg(guild_id: int) -> dict:
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
        if not row:
            return {}
        return {
            "log": row["log_channel_id"],
            "welcome": row["welcome_channel_id"],
            "autorole": row["autorole_id"],
            "ticket_cat": row["ticket_category_id"],
            "anti_links": row["anti_links"] or 0,
        }

async def set_cfg(guild_id: int, **kwargs):
    fields = ["log_channel_id", "welcome_channel_id", "autorole_id", "ticket_category_id", "anti_links"]
    data = {k: kwargs.get(k) for k in fields if k in kwargs}
    with db() as conn:
        c = conn.cursor()
        exists = c.execute("SELECT 1 FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
        if exists:
            sets = ", ".join([f"{k}=?" for k in data])
            if sets:
                c.execute(f"UPDATE guild_config SET {sets} WHERE guild_id=?", (*data.values(), guild_id))
        else:
            cols = ", ".join(["guild_id"] + list(data.keys()))
            qmarks = ", ".join(["?"]*(1+len(data)))
            c.execute(f"INSERT INTO guild_config ({cols}) VALUES ({qmarks})", (guild_id, *data.values()))
        conn.commit()

def staff_check():
    async def predicate(inter: discord.Interaction):
        if inter.user.guild_permissions.manage_guild:
            return True
        raise app_commands.AppCommandError("Você precisa de **Gerenciar Servidor**.")
    return app_commands.check(predicate)

async def send_log(guild: discord.Guild, embed: discord.Embed):
    cfg = await get_cfg(guild.id)
    ch_id = cfg.get("log")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass

# ===================== HELP ===================== #
@bot.tree.command(description=f"Lista os comandos principais do {BRAND}.")
async def help(interaction: discord.Interaction):
    e = discord.Embed(title=f"{BRAND} — Ajuda", color=discord.Color.blurple())
    e.add_field(name="⚙️ Setup", value="`/setup` — configura logs, boas-vindas, autorole, categoria de tickets e anti-links.", inline=False)
    e.add_field(name="🎫 Tickets", value="`/ticketpanel` (cria painel) | `/ticketclose` `/ticketadd` `/ticketremove`", inline=False)
    e.add_field(name="🛡️ Moderação", value="`/ban`, `/unban`, `/kick`, `/mute`, `/unmute`, `/clear`, `/slowmode`, `/warn`, `/warnings`, `/clearwarns`", inline=False)
    e.add_field(name="📈 Níveis", value="`/rank`, `/leaderboard`", inline=False)
    e.add_field(name="💰 Economia", value="`/balance`, `/daily`, `/pay`, `/shop`, `/buy`, `/inventory`", inline=False)
    e.add_field(name="🧩 Cargos", value="`/rolesetup` — menu de cargos", inline=False)
    e.add_field(name="🧰 Utilitários", value="`/ping`, `/serverinfo`, `/userinfo`, `/suggest`, `/poll`", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)

# ===================== SETUP ===================== #
@bot.tree.command(description="Configura canais/cargos do servidor.")
@staff_check()
@app_commands.describe(
    log_channel="Canal de logs",
    welcome_channel="Canal de boas-vindas",
    autorole="Cargo para novos membros",
    ticket_category="Categoria para tickets",
    anti_links="Bloquear links? 0/1"
)
async def setup(
    interaction: discord.Interaction,
    log_channel: Optional[discord.TextChannel] = None,
    welcome_channel: Optional[discord.TextChannel] = None,
    autorole: Optional[discord.Role] = None,
    ticket_category: Optional[discord.CategoryChannel] = None,
    anti_links: Optional[int] = 0
):
    guild = interaction.guild
    assert guild

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Cria canais padrão se não forem passados
    if log_channel is None:
        log_channel = await guild.create_text_channel("logs")
    if welcome_channel is None:
        welcome_channel = await guild.create_text_channel("boas-vindas")
    if ticket_category is None:
        ticket_category = await guild.create_category("TICKETS")

    await set_cfg(
        guild.id,
        log_channel_id=log_channel.id,
        welcome_channel_id=welcome_channel.id,
        autorole_id=autorole.id if autorole else None,
        ticket_category_id=ticket_category.id,
        anti_links=1 if anti_links else 0
    )

    await interaction.followup.send(
        f"✅ **{BRAND} Setup**\n• Logs: {log_channel.mention}\n• Boas-vindas: {welcome_channel.mention}\n• Autorole: {autorole.mention if autorole else '—'}\n• Tickets: {ticket_category.name}\n• Anti-links: {'ativado' if anti_links else 'desativado'}",
        ephemeral=True
    )

# ===================== EVENTOS: WELCOME/LEAVE/LOGS ===================== #
@bot.event
async def on_member_join(member: discord.Member):
    cfg = await get_cfg(member.guild.id)
    ch_id = cfg.get("welcome")
    if ch_id:
        ch = member.guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            e = discord.Embed(description=f"👋 {member.mention} entrou no servidor!", color=discord.Color.green())
            e.set_author(name=f"{BRAND} • Boas-vindas", icon_url=member.display_avatar.url)
            try:
                await ch.send(embed=e)
            except discord.Forbidden:
                pass
    role_id = cfg.get("autorole")
    if role_id:
        role = member.guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.Forbidden:
                pass

@bot.event
async def on_member_remove(member: discord.Member):
    cfg = await get_cfg(member.guild.id)
    ch_id = cfg.get("welcome")
    if ch_id:
        ch = member.guild.get_channel(ch_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(f"👋 {member} saiu do servidor.")
            except discord.Forbidden:
                pass

@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild and not message.author.bot and message.content:
        e = discord.Embed(title="🗑️ Mensagem deletada", description=message.content[:2000], color=discord.Color.red(), timestamp=datetime.utcnow())
        e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        await send_log(message.guild, e)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild and not before.author.bot and (before.content or "") != (after.content or ""):
        e = discord.Embed(title="✏️ Mensagem editada", color=discord.Color.orange(), timestamp=datetime.utcnow())
        e.add_field(name="Antes", value=(before.content or "—")[:1000], inline=False)
        e.add_field(name="Depois", value=(after.content or "—")[:1000], inline=False)
        e.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        await send_log(before.guild, e)

# ===================== ANTI-SPAM / LINKS / XP ===================== #
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    now = time.monotonic()
    times = bot._spam_cache.get(message.author.id, [])
    times = [t for t in times if now - t < SPAM_WINDOW]
    times.append(now)
    bot._spam_cache[message.author.id] = times

    if len(times) > SPAM_LIMIT or (message.mentions and len(message.mentions) >= MENTION_LIMIT):
        try:
            await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=5), reason="Spam/flood")
            await message.channel.send(f"⚠️ {message.author.mention} recebeu timeout por spam (5 min).")
        except discord.Forbidden:
            pass

    cfg = await get_cfg(message.guild.id)
    if cfg.get("anti_links") and ANTI_LINK_REGEX.search(message.content):
        try:
            await message.delete()
            await message.channel.send(f"🚫 {message.author.mention} links não são permitidos aqui.", delete_after=5)
        except discord.Forbidden:
            pass
        return

    # XP cooldown por usuário
    now_ts = datetime.utcnow().timestamp()
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT xp, level, last_msg_ts FROM xp WHERE guild_id=? AND user_id=?", (message.guild.id, message.author.id)).fetchone()
        last = row["last_msg_ts"] if row else 0
        if now_ts - (last or 0) >= MSG_XP_COOLDOWN:
            gained = 5
            xp = (row["xp"] if row else 0) + gained
            level = row["level"] if row else 0
            next_req = (level + 1) * 100
            leveled = False
            if xp >= next_req:
                level += 1
                xp -= next_req
                leveled = True
            c.execute("REPLACE INTO xp (guild_id, user_id, xp, level, last_msg_ts) VALUES (?, ?, ?, ?, ?)",
                      (message.guild.id, message.author.id, xp, level, now_ts))
            conn.commit()
    if 'leveled' in locals() and leveled:
        try:
            await message.channel.send(f"🎉 {message.author.mention} subiu para o nível **{level}**!")
        except discord.Forbidden:
            pass

    await bot.process_commands(message)

# ===================== NÍVEIS ===================== #
@bot.tree.command(description="Mostra nível e XP.")
async def rank(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT xp, level FROM xp WHERE guild_id=? AND user_id=?", (interaction.guild_id, member.id)).fetchone()
    xp = row["xp"] if row else 0
    level = row["level"] if row else 0
    await interaction.response.send_message(f"📈 {member.mention} — Nível **{level}**, XP **{xp}**")

@bot.tree.command(description="Ranking do servidor (Top 10).")
async def leaderboard(interaction: discord.Interaction):
    with db() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT user_id, level, xp FROM xp WHERE guild_id=? ORDER BY level DESC, xp DESC LIMIT 10",
                         (interaction.guild_id,)).fetchall()
    if not rows:
        return await interaction.response.send_message("Sem dados ainda.")
    lines = []
    for i, r in enumerate(rows, start=1):
        user = interaction.guild.get_member(r["user_id"]) or await interaction.client.fetch_user(r["user_id"])  # type: ignore
        name = user.mention if isinstance(user, discord.Member) else user.name
        lines.append(f"**{i}.** {name} — lvl {r['level']} ({r['xp']} XP)")
    await interaction.response.send_message("\n".join(lines))

# ===================== ECONOMIA ===================== #
def _get_inv(row):
    inv_json = row["inv_json"] if row else None
    try:
        return json.loads(inv_json) if inv_json else {}
    except Exception:
        return {}

def _save_econ(guild_id: int, user_id: int, balance: int, inv: dict, last_daily: Optional[str] = None):
    with db() as conn:
        c = conn.cursor()
        c.execute("""REPLACE INTO economy (guild_id, user_id, balance, last_daily, inv_json)
                     VALUES (?, ?, ?, ?, ?)""",
                  (guild_id, user_id, balance, last_daily, json.dumps(inv)))
        conn.commit()

@bot.tree.command(description="Seu saldo.")
async def balance(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT balance FROM economy WHERE guild_id=? AND user_id=?",
                        (interaction.guild_id, member.id)).fetchone()
    bal = row["balance"] if row else 0
    await interaction.response.send_message(f"💰 {member.mention} possui **{bal}** moedas.")

@bot.tree.command(description="Coleta sua recompensa diária.")
async def daily(interaction: discord.Interaction):
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT balance, last_daily, inv_json FROM economy WHERE guild_id=? AND user_id=?",
                        (interaction.guild_id, interaction.user.id)).fetchone()
        bal = row["balance"] if row else 0
        last = datetime.fromisoformat(row["last_daily"]).date() if row and row["last_daily"] else None
        inv = _get_inv(row)
        today = datetime.utcnow().date()
        if last == today:
            return await interaction.response.send_message("⏳ Você já coletou o daily hoje. Volte amanhã!", ephemeral=True)
        bal += DAILY_AMOUNT
        _save_econ(interaction.guild_id, interaction.user.id, bal, inv, datetime.utcnow().isoformat())
    await interaction.response.send_message(f"✅ Você coletou **{DAILY_AMOUNT}** moedas!")

@bot.tree.command(description="Paga um usuário.")
async def pay(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 10_000_000]):
    if member.id == interaction.user.id:
        return await interaction.response.send_message("❌ Não pode pagar a si mesmo.", ephemeral=True)
    with db() as conn:
        c = conn.cursor()
        me = c.execute("SELECT balance, inv_json, last_daily FROM economy WHERE guild_id=? AND user_id=?",
                       (interaction.guild_id, interaction.user.id)).fetchone()
        mybal = me["balance"] if me else 0
        if mybal < amount:
            return await interaction.response.send_message("❌ Saldo insuficiente.", ephemeral=True)
        other = c.execute("SELECT balance, inv_json, last_daily FROM economy WHERE guild_id=? AND user_id=?",
                          (interaction.guild_id, member.id)).fetchone()
        _save_econ(interaction.guild_id, interaction.user.id, mybal - amount, _get_inv(me), me["last_daily"] if me else None)
        _save_econ(interaction.guild_id, member.id, (other["balance"] if other else 0) + amount, _get_inv(other), other["last_daily"] if other else None)
    await interaction.response.send_message(f"✅ Pagou **{amount}** para {member.mention}.")

@bot.tree.command(description="Lista itens da loja.")
async def shop(interaction: discord.Interaction):
    lines = [f"• **{name}** — {price} moedas" for name, price in SHOP_ITEMS.items()]
    await interaction.response.send_message("🛒 **Loja**\n" + "\n".join(lines))

@bot.tree.command(description="Compra um item da loja.")
async def buy(interaction: discord.Interaction, item: str):
    item = item.lower()
    if item not in SHOP_ITEMS:
        return await interaction.response.send_message("Item não existe.", ephemeral=True)
    cost = SHOP_ITEMS[item]
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT balance, inv_json, last_daily FROM economy WHERE guild_id=? AND user_id=?",
                        (interaction.guild_id, interaction.user.id)).fetchone()
        bal = row["balance"] if row else 0
        if bal < cost:
            return await interaction.response.send_message("Saldo insuficiente.", ephemeral=True)
        inv = _get_inv(row)
        inv[item] = inv.get(item, 0) + 1
        _save_econ(interaction.guild_id, interaction.user.id, bal - cost, inv, row["last_daily"] if row else None)
    await interaction.response.send_message(f"🛍️ Você comprou **{item}** por {cost} moedas.")

@bot.tree.command(description="Mostra seus itens.")
async def inventory(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT inv_json FROM economy WHERE guild_id=? AND user_id=?",
                        (interaction.guild_id, member.id)).fetchone()
    inv = _get_inv(row)
    if not inv:
        return await interaction.response.send_message("Inventário vazio.")
    lines = [f"• {k} x{v}" for k, v in inv.items()]
    await interaction.response.send_message("🎒 **Inventário**\n" + "\n".join(lines))

# ===================== MODERAÇÃO E WARNS ===================== #
@bot.tree.command(description="Expulsa um membro.")
@staff_check()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 {member} foi expulso. Motivo: {reason or '—'}")
    await send_log(interaction.guild, discord.Embed(description=f"👢 {member} expulso por {interaction.user}. Motivo: {reason or '—'}"))

@bot.tree.command(description="Bane um membro.")
@staff_check()
async def ban(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🔨 {member} foi banido. Motivo: {reason or '—'}")
    await send_log(interaction.guild, discord.Embed(description=f"🔨 {member} banido por {interaction.user}. Motivo: {reason or '—'}"))

@bot.tree.command(description="Desbane por ID.")
@staff_check()
async def unban(interaction: discord.Interaction, user_id: int):
    user = await bot.fetch_user(user_id)
    await interaction.guild.unban(user)  # type: ignore
    await interaction.response.send_message(f"♻️ {user} desbanido.")
    await send_log(interaction.guild, discord.Embed(description=f"♻️ {user} desbanido por {interaction.user}."))

@bot.tree.command(description="Aplicar timeout (mute).")
@staff_check()
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 43200] = 10, reason: Optional[str] = None):
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    await member.timeout(until=until, reason=reason)
    await interaction.response.send_message(f"🔇 {member.mention} mutado por {minutes} min. {('Motivo: '+reason) if reason else ''}")
    await send_log(interaction.guild, discord.Embed(description=f"🔇 {member} timeout por {minutes} min. Por {interaction.user}. Motivo: {reason or '—'}"))

@bot.tree.command(description="Remove timeout.")
@staff_check()
async def unmute(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(until=None)
    await interaction.response.send_message(f"🔈 {member.mention} desmutado.")
    await send_log(interaction.guild, discord.Embed(description=f"🔈 {member} desmutado por {interaction.user}."))

@bot.tree.command(description="Limpa mensagens no canal.")
@staff_check()
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500] = 10):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)  # type: ignore
    await interaction.followup.send(f"🧹 Apagadas {len(deleted)} mensagens.", ephemeral=True)

@bot.tree.command(description="Define slowmode do canal.")
@staff_check()
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    channel = interaction.channel
    assert isinstance(channel, discord.TextChannel)
    await channel.edit(slowmode_delay=seconds)
    await interaction.response.send_message(f"🐢 Slowmode definido para {seconds}s.")

@bot.tree.command(description="Aplica advertência a um membro.")
@staff_check()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO warns (guild_id, user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (interaction.guild_id, member.id, interaction.user.id, reason, datetime.utcnow().isoformat()))
        conn.commit()
    await interaction.response.send_message(f"⚠️ {member.mention} advertido: {reason}")
    await send_log(interaction.guild, discord.Embed(description=f"⚠️ {member} advertido por {interaction.user}: {reason}"))

@bot.tree.command(description="Lista advertências de um membro.")
@staff_check()
async def warnings(interaction: discord.Interaction, member: discord.Member):
    with db() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT moderator_id, reason, timestamp FROM warns WHERE guild_id=? AND user_id=? ORDER BY timestamp DESC",
                         (interaction.guild_id, member.id)).fetchall()
    if not rows:
        return await interaction.response.send_message("✅ Sem advertências.")
    lines = [f"• {datetime.fromisoformat(r['timestamp']).strftime('%d/%m/%Y %H:%M')} — <@{r['moderator_id']}>: {r['reason']}" for r in rows]
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(description="Remove todas as advertências de um membro.")
@staff_check()
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    with db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM warns WHERE guild_id=? AND user_id=?", (interaction.guild_id, member.id))
        conn.commit()
    await interaction.response.send_message(f"🧽 Advertências de {member.mention} foram limpas.")

# ===================== TICKETS (CORRIGIDO E PERSISTENTE) ===================== #
class TicketPanelView(discord.ui.View):
    """View persistente com botões de abrir/fechar ticket.
    - Custom IDs fixos garantem que a view reanexe após reboot.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir Ticket", style=discord.ButtonStyle.green, custom_id="k1ll_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        assert guild is not None

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Verificação de permissões do bot
        me = guild.me
        if not (me.guild_permissions.manage_channels and me.guild_permissions.manage_roles):
            return await interaction.followup.send("❌ O bot precisa de **Manage Channels** e **Manage Roles**.", ephemeral=True)

        cfg = await get_cfg(guild.id)
        cat_id = cfg.get("ticket_cat")
        if not cat_id:
            return await interaction.followup.send("Sistema de tickets não configurado. Use `/setup`.", ephemeral=True)
        category = guild.get_channel(cat_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send("Categoria de tickets inválida. Refaça o `/setup`.", ephemeral=True)

        # Previne duplicado
        with db() as conn:
            c = conn.cursor()
            row = c.execute("SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND open=1",
                            (guild.id, interaction.user.id)).fetchone()
            if row:
                ch = guild.get_channel(row["channel_id"])
                return await interaction.followup.send(f"Você já tem um ticket aberto: {ch.mention if ch else '#apagado'}", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, attach_files=True)
        }
        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites,
            reason=f"Ticket de {interaction.user}"
        )
        with db() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO tickets (guild_id, user_id, channel_id, open, created_at) VALUES (?, ?, ?, 1, ?)",
                      (guild.id, interaction.user.id, channel.id, datetime.utcnow().isoformat()))
            conn.commit()

        # botão de fechar dentro do ticket (usamos comando também)
        close_btn = discord.ui.Button(label="Fechar Ticket", style=discord.ButtonStyle.red, custom_id="k1ll_ticket_close_inline")

        async def _close(_inter: discord.Interaction):
            if _inter.user.id != interaction.user.id and not _inter.user.guild_permissions.manage_channels:
                return await _inter.response.send_message("Apenas o autor ou staff podem fechar.", ephemeral=True)
            await _inter.response.defer(ephemeral=True, thinking=True)
            await ticket_close_logic(guild, channel, _inter)

        close_btn.callback = _close
        v = discord.ui.View()
        v.add_item(close_btn)

        await channel.send(f"{interaction.user.mention} Ticket criado. Explique seu problema.\nStaff pode usar `/ticketadd` para adicionar alguém.", view=v)
        await interaction.followup.send(f"✅ Ticket criado: {channel.mention}", ephemeral=True)

    @discord.ui.button(label="Fechar Ticket (aqui)", style=discord.ButtonStyle.red, custom_id="k1ll_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("Use dentro do canal do ticket.", ephemeral=True)
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ticket_close_logic(guild, ch, interaction)

async def ticket_close_logic(guild: discord.Guild, ch: discord.TextChannel, interaction: discord.Interaction):
    with db() as conn:
        c = conn.cursor()
        row = c.execute("SELECT user_id FROM tickets WHERE guild_id=? AND channel_id=? AND open=1", (guild.id, ch.id)).fetchone()
        if not row:
            return await interaction.followup.send("Este canal não é um ticket aberto.", ephemeral=True)
        owner_id = row["user_id"]

    if interaction.user.id != owner_id and not interaction.user.guild_permissions.manage_channels:
        return await interaction.followup.send("Apenas o autor ou staff podem fechar este ticket.", ephemeral=True)

    # Transcrição
    lines = []
    async for m in ch.history(limit=None, oldest_first=True):
        content = (m.content or "").replace("\n", " ")
        lines.append(f"[{m.created_at:%d/%m %H:%M}] {m.author}: {content}")
    transcript = "\n".join(lines) or "Sem mensagens."
    buf = io.BytesIO(transcript.encode("utf-8"))
    buf.seek(0)

    with db() as conn:
        c = conn.cursor()
        c.execute("UPDATE tickets SET open=0 WHERE guild_id=? AND channel_id=?", (guild.id, ch.id))
        conn.commit()

    await ch.send(file=discord.File(buf, filename=f"transcript_{ch.id}.txt"))
    await interaction.followup.send("🔒 Ticket fechado. Você pode arquivar/deletar este canal.", ephemeral=True)

# Comandos de staff para tickets
@bot.tree.command(description="Cria/atualiza o painel de tickets no canal atual.")
@staff_check()
async def ticketpanel(interaction: discord.Interaction):
    embed = discord.Embed(title="🎫 Atendimento — k1LL", description="Clique em **Abrir Ticket** para falar com a staff.", color=discord.Color.green())
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=embed, view=TicketPanelView())  # type: ignore
    await interaction.followup.send("✅ Painel criado/atualizado.", ephemeral=True)

@bot.tree.command(description="(Staff) Fecha o ticket atual com transcript.")
@staff_check()
async def ticketclose(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("Use no canal do ticket.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    await ticket_close_logic(interaction.guild, ch, interaction)  # type: ignore

@bot.tree.command(description="(Staff) Adiciona um membro ao ticket atual.")
@staff_check()
async def ticketadd(interaction: discord.Interaction, member: discord.Member):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("Use no canal do ticket.", ephemeral=True)
    await ch.set_permissions(member, view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
    await interaction.response.send_message(f"✅ {member.mention} adicionado ao ticket.", ephemeral=True)

@bot.tree.command(description="(Staff) Remove um membro do ticket atual.")
@staff_check()
async def ticketremove(interaction: discord.Interaction, member: discord.Member):
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("Use no canal do ticket.", ephemeral=True)
    await ch.set_permissions(member, overwrite=None)
    await interaction.response.send_message(f"✅ {member.mention} removido do ticket.", ephemeral=True)

# ===================== REACTION ROLES (MENU) ===================== #
class RoleMenu(discord.ui.Select):
    def __init__(self, roles: list[discord.Role]):
        options = [discord.SelectOption(label=r.name, value=str(r.id)) for r in roles[:25]]
        super().__init__(placeholder="Escolha seus cargos", min_values=0, max_values=len(options) if options else 1, options=options, custom_id="k1ll_rolemenu")

    async def callback(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        chosen_ids = set(int(v) for v in self.values) if self.values else set()
        menu_role_ids = [int(opt.value) for opt in self.options]

        # Remove todos do conjunto do menu
        for rid in menu_role_ids:
            role = interaction.guild.get_role(rid)
            if role and role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(role, reason="Role menu toggle")
                except discord.Forbidden:
                    pass

        # Adiciona os escolhidos
        for rid in chosen_ids:
            role = interaction.guild.get_role(rid)
            if role:
                try:
                    await interaction.user.add_roles(role, reason="Role menu toggle")
                except discord.Forbidden:
                    pass
        await interaction.response.send_message("✅ Cargos atualizados!", ephemeral=True)

class RoleMenuView(discord.ui.View):
    def __init__(self, roles: list[discord.Role]):
        super().__init__(timeout=None)
        self.add_item(RoleMenu(roles))

class RoleMenuPersist(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleMenu([]))

@bot.tree.command(description="Cria menu de cargos (até 25). Passe menções/IDs separados por espaço.")
@staff_check()
async def rolesetup(interaction: discord.Interaction, roles: str):
    guild = interaction.guild
    assert guild is not None
    ids: list[int] = []
    for token in roles.replace("<@&", "").replace(">", "").split():
        if token.isdigit():
            ids.append(int(token))
    role_objs = [guild.get_role(rid) for rid in ids]
    role_objs = [r for r in role_objs if r and not r.managed]
    if not role_objs:
        return await interaction.response.send_message("Forneça cargos válidos (menções ou IDs).", ephemeral=True)
    await interaction.channel.send("Selecione seus cargos:", view=RoleMenuView(role_objs))  # type: ignore
    await interaction.response.send_message("✅ Menu criado.", ephemeral=True)

# ===================== UTILITÁRIOS ===================== #
@bot.tree.command(description="Mostra latência do bot.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 {BRAND} Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(description="Informações do servidor.")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    assert g is not None
    e = discord.Embed(title=f"{g.name} — {BRAND}", color=discord.Color.blurple())
    e.add_field(name="Membros", value=g.member_count)
    e.add_field(name="Canais", value=len(g.channels))
    e.add_field(name="Criado em", value=g.created_at.strftime("%d/%m/%Y"))
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    await interaction.response.send_message(embed=e)

@bot.tree.command(description="Informações de um usuário.")
async def userinfo(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    e = discord.Embed(title=str(member), color=discord.Color.blurple())
    e.add_field(name="Entrou em", value=member.joined_at.strftime("%d/%m/%Y") if member.joined_at else "—")
    e.add_field(name="Criado em", value=member.created_at.strftime("%d/%m/%Y"))
    e.add_field(name="ID", value=str(member.id))
    e.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=e)

@bot.tree.command(description="Enviar uma sugestão para o canal atual.")
async def suggest(interaction: discord.Interaction, mensagem: str):
    e = discord.Embed(title=f"{BRAND} • Sugestão", description=mensagem, color=discord.Color.gold())
    e.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    msg = await interaction.channel.send(embed=e)  # type: ignore
    for emo in ("👍", "👎"):
        try:
            await msg.add_reaction(emo)
        except discord.HTTPException:
            pass
    await interaction.response.send_message("✅ Sugestão enviada!", ephemeral=True)

@bot.tree.command(description="Cria uma enquete rápida com 2–5 opções.")
async def poll(interaction: discord.Interaction, pergunta: str, op1: str, op2: str, op3: Optional[str] = None, op4: Optional[str] = None, op5: Optional[str] = None):
    options = [op1, op2] + [o for o in [op3, op4, op5] if o]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    desc = "\n".join(f"{emojis[i]} {opt}" for i, opt in enumerate(options))
    e = discord.Embed(title="📊 Enquete", description=f"**{pergunta}**\n\n{desc}")
    msg = await interaction.channel.send(embed=e)  # type: ignore
    for i in range(len(options)):
        try:
            await msg.add_reaction(emojis[i])
        except discord.HTTPException:
            pass
    await interaction.response.send_message("✅ Enquete criada.", ephemeral=True)

# ===================== ERROS GLOBAIS ===================== #
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        err = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        await interaction.response.send_message(f"❌ Erro: {err}", ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(f"❌ Erro: {err}", ephemeral=True)

# ===================== MAIN ===================== #
def main():
    if not TOKEN:
        print("❌ Defina DISCORD_TOKEN no ambiente ou no .env.")
        return
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
