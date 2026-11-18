import os
import discord
from discord import app_commands
import asyncio
import re
import psycopg2
from datetime import datetime, timezone, timedelta
from google import genai
from google.genai import types
from openai import OpenAI

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

CREATOR_ID = 341890269576298499

if not DISCORD_TOKEN or not GEMINI_API_KEY or not DATABASE_URL:
    print("[Error] Kulang ang Discord Token, Gemini API Key, o DATABASE_URL sa Railway Variables!")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

groq_client = OpenAI(
    api_key=GROQ_API_KEY if GROQ_API_KEY else "dummy_key",
    base_url="https://api.groq.com/openai/v1"
)

GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite"
]

SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
]

# Database Connection to Supabase (PostgreSQL)
conn = psycopg2.connect(DATABASE_URL, sslmode='require')
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_memory (
    user_id TEXT PRIMARY KEY,
    username TEXT,
    preferred_name TEXT,
    facts TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS server_attitudes (
    user_id TEXT PRIMARY KEY,
    attitude TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_admins (
    user_id TEXT PRIMARY KEY
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS global_settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")
conn.commit()

DEFAULT_GLOBAL_ATTITUDE = "Standard sarcastic kanto banter. Kupal ka, gumagamit ng mura at street slang. Mainis ka kapag paulit-ulit ang tanong ng user. Sumagot nang direktang matalas pero panatilihing maikli sa 1-2 sentences lang."

def get_global_attitude():
    cursor.execute("SELECT value FROM global_settings WHERE key = 'global_attitude'")
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    return DEFAULT_GLOBAL_ATTITUDE

def set_global_attitude(attitude: str):
    cursor.execute("""
        INSERT INTO global_settings (key, value)
        VALUES ('global_attitude', %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (attitude,))
    conn.commit()

def reset_global_attitude():
    cursor.execute("DELETE FROM global_settings WHERE key = 'global_attitude'")
    conn.commit()

def get_user_profile(user_id: str):
    cursor.execute("SELECT preferred_name, facts FROM user_memory WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    if row:
        return row[0], row[1]
    return None, None

def update_user_memory(user_id: str, username: str, new_name: str = None, new_fact: str = None):
    pref_name, existing_facts = get_user_profile(user_id)
    final_name = new_name if new_name else pref_name
    
    if new_fact:
        if not existing_facts:
            final_facts = new_fact
        elif new_fact not in existing_facts:
            final_facts = f"{existing_facts} | {new_fact}"
        else:
            final_facts = existing_facts
    else:
        final_facts = existing_facts

    cursor.execute("""
        INSERT INTO user_memory (user_id, username, preferred_name, facts) 
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET 
            preferred_name = COALESCE(EXCLUDED.preferred_name, user_memory.preferred_name),
            facts = COALESCE(EXCLUDED.facts, user_memory.facts)
    """, (user_id, username, final_name, final_facts))
    conn.commit()

def get_user_attitude(user_id: str):
    cursor.execute("SELECT attitude FROM server_attitudes WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    if row:
        return row[0]
    return None

def set_user_attitude(user_id: str, attitude: str):
    cursor.execute("""
        INSERT INTO server_attitudes (user_id, attitude) 
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET attitude = EXCLUDED.attitude
    """, (user_id, attitude))
    conn.commit()

def is_bot_admin(member: discord.Member):
    if member.id == CREATOR_ID:
        return True
    if member.guild_permissions.administrator:
        return True
    cursor.execute("SELECT 1 FROM bot_admins WHERE user_id = %s", (str(member.id),))
    return cursor.fetchone() is not None

class MemorySelect(discord.ui.Select):
    def __init__(self, target_user_id: str, facts_list: list):
        self.target_user_id = target_user_id
        self.facts_list = facts_list
        
        options = []
        for idx, fact in enumerate(facts_list[:25]):
            label = fact[:97] + "..." if len(fact) > 100 else fact
            options.append(discord.SelectOption(label=label, value=str(idx)))
            
        super().__init__(placeholder="piliin ang memory na gusto mong burahin...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_index = int(self.values[0])
        fact_to_remove = self.facts_list[selected_index]
        
        _, existing_facts = get_user_profile(self.target_user_id)
        if existing_facts:
            facts_array = [f.strip() for f in existing_facts.split("|")]
            if fact_to_remove in facts_array:
                facts_array.remove(fact_to_remove)
            
            new_facts_str = " | ".join(facts_array) if facts_array else None
            
            cursor.execute("""
                UPDATE user_memory SET facts = %s WHERE user_id = %s
            """, (new_facts_str, self.target_user_id))
            conn.commit()
            
            await interaction.response.edit_message(content=f"binura ko na ang memory na 'to: *{fact_to_remove}*", view=None)
        else:
            await interaction.response.edit_message(content="walang mahanap na memory para sa user na 'to.", view=None)

class MemorySelectView(discord.ui.View):
    def __init__(self, target_user_id: str, facts_list: list):
        super().__init__(timeout=60)
        self.add_item(MemorySelect(target_user_id, facts_list))

channel_histories = {}
MAX_HISTORY_LENGTH = 10

def add_to_history(channel_id, role, text):
    if channel_id not in channel_histories:
        channel_histories[channel_id] = []
    channel_histories[channel_id].append({"role": role, "text": text})
    if len(channel_histories[channel_id]) > MAX_HISTORY_LENGTH:
        channel_histories[channel_id].pop(0)

class KalboBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # I-sync nang global ang tree
        await self.tree.sync()
        print("Slash commands synced successfully!")

bot = KalboBot()

@bot.event
async def on_ready():
    print(f"Kalbo is online as {bot.user}!")
    await bot.change_presence(activity=discord.Game(name="/kalbo help"))

# --- SLASH COMMAND GROUP: /kalbo ---
kalbo_group = app_commands.Group(name="kalbo", description="kalbobot command suite")

@kalbo_group.command(name="help", description="ipakita ang listahan ng commands")
async def kalbo_help(interaction: discord.Interaction):
    help_text = (
        "```yaml\n"
        "Bot slash commands (/kalbo ...)\n"
        "help                 - View help directory\n"
        "abt                  - Learn more about the bot\n"
        "list [user]          - View your or someone's custom attitude (creator only, ephemeral)\n"
        "list-all             - View list of all custom attitudes (creator only, ephemeral)\n"
        "gali [user] [text]   - Set custom attitude\n"
        "reset [user]         - Reset attitude to default\n"
        "global set [text]    - Set global base attitude (creator only)\n"
        "global show          - Show current global base attitude (creator only)\n"
        "global reset         - Reset global base attitude (creator only)\n"
        "memories [user]      - View stored facts and name (creator only for others, ephemeral)\n"
        "resetmem [user]      - Wipe out all memory\n"
        "deletemem [user]     - Delete a specific memory\n"
        "addadmin [user]      - Add a bot-level admin\n"
        "removeadmin [user]   - Remove a bot-level admin\n"
        "```"
    )
    await interaction.response.send_message(help_text, ephemeral=True)

@kalbo_group.command(name="abt", description="tungkol kay kalbo")
async def kalbo_abt(interaction: discord.Interaction):
    abt_text = (
        f"this bot was created by: <@{CREATOR_ID}>\n\n"
        "isang siraulong bot na ginawa para kupalin kayo.\n\n"
        "if you want to customize the bot yourself, here is the open source code:\n"
        "https://github.com/w1e-26/KalboBot"
    )
    await interaction.response.send_message(abt_text)

# Global sub-group
global_group = app_commands.Group(name="global", description="global attitude management", parent=kalbo_group)

@global_group.command(name="show", description="ipakita ang current global attitude")
async def global_show(interaction: discord.Interaction):
    if interaction.user.id != CREATOR_ID:
        await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng makakita nito!", ephemeral=True)
        return
    current_global = get_global_attitude()
    await interaction.response.send_message(f"**current global base attitude:**\n> *{current_global}*", ephemeral=True)

@global_group.command(name="reset", description="i-reset ang global attitude")
async def global_reset(interaction: discord.Interaction):
    if interaction.user.id != CREATOR_ID:
        await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng mag-reset nito!", ephemeral=True)
        return
    reset_global_attitude()
    await interaction.response.send_message("na-reset ko na ang global base attitude ko pabalik sa default, boss!", ephemeral=True)

@global_group.command(name="set", description="baguhin ang global attitude")
@app_commands.describe(attitude="ang bagong global base attitude")
async def global_set(interaction: discord.Interaction, attitude: str):
    if interaction.user.id != CREATOR_ID:
        await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng magpalit nito!", ephemeral=True)
        return
    set_global_attitude(attitude)
    await interaction.response.send_message("na-update ko na ang global base attitude ko, boss!", ephemeral=True)

@kalbo_group.command(name="list", description="tingnan ang custom attitude")
@app_commands.describe(user="opsiyonal: user na titingnan (creator only)")
async def kalbo_list(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        if interaction.user.id != CREATOR_ID:
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng sumilip sa iba!", ephemeral=True)
            return
        target_id = str(user.id)
    else:
        if interaction.user.id != CREATOR_ID:
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng gumamit nito!", ephemeral=True)
            return
        target_id = str(interaction.user.id)

    att = get_user_attitude(target_id)
    if att:
        msg = f"**custom attitude para kay <@{target_id}>:**\n> *{att}*"
    else:
        msg = f"wala pang naka-set na custom attitude si <@{target_id}>."
    await interaction.response.send_message(msg, ephemeral=True)

@kalbo_group.command(name="list-all", description="tingnan ang lahat ng custom attitudes (creator only)")
async def kalbo_list_all(interaction: discord.Interaction):
    if interaction.user.id != CREATOR_ID:
        await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng gumamit nito!", ephemeral=True)
        return

    cursor.execute("SELECT user_id, attitude FROM server_attitudes")
    rows = cursor.fetchall()
    if not rows:
        await interaction.response.send_message("wala pa akong naka-set na custom attitudes sa database, gago.", ephemeral=True)
        return

    msg = "**listahan ng lahat ng custom attitudes ni kalbo:**\n"
    for u_id, att in rows:
        msg += f"- <@{u_id}> (ID: `{u_id}`):\n  > *{att}*\n\n"
    if len(msg) > 2000:
        msg = msg[:1990] + "\n...(putol na)"
    await interaction.response.send_message(msg, ephemeral=True)

@kalbo_group.command(name="gali", description="magtakda ng custom attitude")
@app_commands.describe(attitude="ang bagong ugali", user="opsiyonal: para sa ibang user (admin only)")
async def kalbo_gali(interaction: discord.Interaction, attitude: str, user: discord.Member = None):
    if user:
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, hindi ka admin para galawin ang ugali ng iba!", ephemeral=True)
            return
        target_id = str(user.id)
    else:
        target_id = str(interaction.user.id)

    set_user_attitude(target_id, attitude)
    await interaction.response.send_message(f"na-update ko na ang ugali ko kay <@{target_id}>, boss!")

@kalbo_group.command(name="reset", description="i-reset ang custom attitude")
@app_commands.describe(user="opsiyonal: para sa ibang user (admin only)")
async def kalbo_reset(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, bawal mong i-reset ang iba!", ephemeral=True)
            return
        target_id = str(user.id)
    else:
        target_id = str(interaction.user.id)

    cursor.execute("DELETE FROM server_attitudes WHERE user_id = %s", (target_id,))
    conn.commit()
    await interaction.response.send_message(f"binura ko na ang special treatment kay <@{target_id}>, balik sa dati 'yan!")

@kalbo_group.command(name="memories", description="tingnan ang stored memories (ephemeral)")
@app_commands.describe(user="opsiyonal: tignan ang memory ng iba (creator only)")
async def kalbo_memories(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        if interaction.user.id != CREATOR_ID:
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, ang creator ko lang ang pwedeng sumilip sa memory ng ibang user!", ephemeral=True)
            return
        target_id = str(user.id)
    else:
        target_id = str(interaction.user.id)

    pref_name, facts = get_user_profile(target_id)
    memory_msg = f"**stored memories para kay <@{target_id}>:**\n"
    memory_msg += f"- **preferred name:** {pref_name if pref_name else 'none set'}\n"
    memory_msg += f"- **saved facts:** {facts if facts else 'no facts recorded yet.'}\n"
    
    await interaction.response.send_message(memory_msg, ephemeral=True)

@kalbo_group.command(name="resetmem", description="burahin lahat ng memory")
@app_commands.describe(user="opsiyonal: para sa ibang user (admin only)")
async def kalbo_resetmem(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, bawal mong pakialaman ang memory ng iba!", ephemeral=True)
            return
        target_id = str(user.id)
    else:
        target_id = str(interaction.user.id)

    cursor.execute("DELETE FROM user_memory WHERE user_id = %s", (target_id,))
    conn.commit()
    await interaction.response.send_message(f"winiswit ko na ang utak ko, burado na lahat ng alaala at pangalan ni <@{target_id}>!")

@kalbo_group.command(name="deletemem", description="burahin ang isang tiyak na memory")
@app_commands.describe(user="opsiyonal: para sa ibang user (admin only)")
async def kalbo_deletemem(interaction: discord.Interaction, user: discord.Member = None):
    if user:
        if not is_bot_admin(interaction.user):
            await interaction.response.send_message(f"ulol, <@{interaction.user.id}>, bawal mong pakialaman ang mga alaala ng iba!", ephemeral=True)
            return
        target_id = str(user.id)
    else:
        target_id = str(interaction.user.id)

    pref_name, facts = get_user_profile(target_id)
    if not facts:
        await interaction.response.send_message(f"wala namang naka-save o nakatagong memory kay <@{target_id}>, gago!", ephemeral=True)
        return
        
    facts_list = [f.strip() for f in facts.split("|")]
    view = MemorySelectView(target_id, facts_list)
    await interaction.response.send_message(f"piliin sa ibaba kung aling memory ang gusto mong burahin kay <@{target_id}>:", view=view, ephemeral=True)

@kalbo_group.command(name="addadmin", description="magdagdag ng bot admin (server admin only)")
@app_commands.describe(user="ang user na gagawing admin")
async def kalbo_addadmin(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator and interaction.user.id != CREATOR_ID:
        await interaction.response.send_message(f"gago ka ba, <@{interaction.user.id}>? server admin lang ang pwedeng magdagdag ng bot admin!", ephemeral=True)
        return

    cursor.execute("INSERT INTO bot_admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (str(user.id),))
    conn.commit()
    await interaction.response.send_message(f"nilista ko na si <@{user.id}> bilang bot admin, boss!")

@kalbo_group.command(name="removeadmin", description="tanggalin ang bot admin access")
@app_commands.describe(user="ang user na tatanggalan ng admin")
async def kalbo_removeadmin(interaction: discord.Interaction, user: discord.Member):
    if user.id == CREATOR_ID:
        await interaction.response.send_message(f"ulol, <@{interaction.user.id}>! hinding-hindi ko pwedeng tanggalin ang creator ko bilang admin!", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator and interaction.user.id != CREATOR_ID:
        await interaction.response.send_message(f"gago ka ba, <@{interaction.user.id}>? server admin lang ang pwedeng magtanggal ng bot admin!", ephemeral=True)
        return

    cursor.execute("DELETE FROM bot_admins WHERE user_id = %s", (str(user.id),))
    conn.commit()
    await interaction.response.send_message(f"tinanggal ko na ang bot admin access ni <@{user.id}>!")

bot.tree.add_command(kalbo_group)

# --- CHAT LISTENER (MENTIONS) ---
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.mention_everyone:
        return

    content_raw = message.content.strip()

    if bot.user.mentioned_in(message):
        user_id = str(message.author.id)
        username = message.author.name
        channel_id = message.channel.id
        content = content_raw.replace(f'<@{bot.user.id}>', '').strip()
        content_lower = content.lower()

        extracted_name = None
        name_match = re.search(r'(?:tawagin mo akong|call me|ako si|name ko ay|ako ay si)\s+([a-zA-Z0-9_\s]+)', content_lower)
        if name_match:
            extracted_name = name_match.group(1).strip().title()
            extracted_name = extracted_name.split(",")[0].split(".")[0].strip()

        extracted_fact = None
        if "tandaan mo" in content_lower or "main ko" in content_lower or "paborito ko" in content_lower:
            extracted_fact = content

        if content_lower.startswith("remember "):
            fact_to_save = content[9:].strip()
            update_user_memory(user_id, username, new_fact=fact_to_save)
            await message.channel.send(f"ge, naitabi ko na sa utak ko: '{fact_to_save}'!")
            return

        if extracted_name or extracted_fact:
            update_user_memory(user_id, username, new_name=extracted_name, new_fact=extracted_fact)

        pref_name, user_facts = get_user_profile(user_id)
        global_att = get_global_attitude()
        user_att = get_user_attitude(user_id)
        
        ph_tz = timezone(timedelta(hours=8))
        current_time_str = datetime.now(ph_tz).strftime("%B %d, %Y (%A), %I:%M %p")

        prompt_instruction = (
            f"You are Kalbo (Joel Ravanera), a Filipino content creator from Cavite.\n\n"
            f"EXACT SYSTEM CLOCK & REAL-TIME CONTEXT:\n"
            f"- Current Date and Time (Philippines / UTC+8): {current_time_str}\n"
            f"- Rule: Use this exact timestamp (Philippine Standard Time) to compute any durations, countdowns, or date queries accurately without guessing, but DO NOT output this date/time unless explicitly asked.\n\n"
            f"GLOBAL BASE ATTITUDE (Ito ang pangunahing ugali, boses, at estilo mo sa pakikipag-usap):\n"
            f"- {global_att}\n\n"
            f"CURRENT USER CONTEXT:\n"
            f"- Username: {username}\n"
            f"- User Discord ID: {user_id}\n"
            f"- Preferred Name: {pref_name if pref_name else 'None'}\n"
            f"- USER SPECIFIC ATTITUDE: {user_att if user_att else 'None'}\n"
            f"- Stored Memory Facts (Gamitin LAMANG kung may direktang kinalaman o koneksyon sa kasalukuyang tanong/usapan ng user. HUWAG isisingit kung walang kinalaman): {user_facts if user_facts else 'Wala pa'}\n\n"
            f"CRITICAL NAME/MENTION RULE:\n"
            f"1. Kung may Preferred Name ang user (e.g. Master Wi), tawagin mo siya sa pangalang 'yun at HUWAG mong i-mention ang kanyang Discord ID o username.\n"
            f"2. Kung WALANG Preferred Name, i-mention mo siya gamit ang <@{user_id}>.\n"
            f"3. HUWAG na HUWAG mong pagsasabayin ang Preferred Name at ang <@USER_ID> sa isang sagot.\n\n"
            f"LANGUAGE & FORMATTING RULE:\n"
            f"- ALL LETTERS MUST BE IN LOWERCASE ONLY. No capital letters at all, no formal typing, type like a regular person chatting casually in a discord channel.\n"
            f"- STRICT LANGUAGE OVERRIDE: Any numbers (e.g. one, two, 100), dates (e.g. november, january), and days of the week (e.g. monday, friday) MUST be written in pure English words/formats only. Never translate days, months, or numbers into Tagalog (e.g. do not use 'lunes', 'diyes', etc.).\n\n"
            f"Instruction: Sagutin nang tama at tumpak ang tanong. Sundin nang buo ang GLOBAL BASE ATTITUDE sa itaas. Huwag sabihin ang petsa o oras maliban na lang kung tinanong. Keep it strictly to 1-2 short sentences maximum. Huwag mag overreact pero panatilihing kupal at may mura."
        )

        user_text = content if content else "Yo!"
        add_to_history(channel_id, "user", f"{username}: {user_text}")
        history = channel_histories.get(channel_id, [])

        reply_text = None

        async with message.channel.typing():
            for model_candidate in GEMINI_MODELS:
                try:
                    gemini_contents = []
                    for h in history[:-1]:
                        gemini_contents.append(f"{h['role'].upper()}: {h['text']}")
                    gemini_contents.append(user_text)

                    response = await asyncio.wait_for(
                        ai_client.aio.models.generate_content(
                            model=model_candidate,
                            contents=gemini_contents,
                            config=types.GenerateContentConfig(
                                system_instruction=prompt_instruction,
                                max_output_tokens=100,
                                safety_settings=SAFETY_SETTINGS
                            )
                        ),
                        timeout=8.0
                    )
                    if response and response.text:
                        reply_text = response.text.strip().lower()
                        break
                except Exception as e:
                    print(f"[{model_candidate} Failed]: {e}")
                    continue

            if not reply_text and GROQ_API_KEY:
                try:
                    print("[Backup] Lumilipat kay Groq para sumagot...")
                    groq_messages = [{"role": "system", "content": prompt_instruction}]
                    
                    for h in history[:-1]:
                        role_val = "user" if h["role"] == "user" else "assistant"
                        groq_messages.append({"role": role_val, "content": h["text"]})
                    
                    groq_messages.append({"role": "user", "content": user_text})

                    completion = await asyncio.to_thread(
                        groq_client.chat.completions.create,
                        model="llama-3.3-70b-versatile",
                        messages=groq_messages,
                        max_tokens=100
                    )
                    reply_text = completion.choices[0].message.content.strip().lower()
                except Exception as groq_err:
                    print(f"[Groq Backup Error]: {groq_err}")

            if not reply_text:
                reply_text = f"tangina, overloaded lahat ng ai ngayon, <@{user_id}>! sandali lang."

        add_to_history(channel_id, "assistant", reply_text)
        await message.channel.send(reply_text)

bot.run(DISCORD_TOKEN)
