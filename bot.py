import os
import sqlite3
import discord
import asyncio
import re
from datetime import datetime
from google import genai
from google.genai import types
from openai import OpenAI

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") 

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    print("[Error] Kulang ang Discord Token o Gemini API Key sa Railway Variables!")

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

conn = sqlite3.connect("kalbo_memory.db")
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
conn.commit()

cursor.execute("PRAGMA table_info(user_memory)")
columns = [column[1] for column in cursor.fetchall()]
if "preferred_name" not in columns:
    cursor.execute("ALTER TABLE user_memory ADD COLUMN preferred_name TEXT")
    conn.commit()

def get_user_profile(user_id: str):
    cursor.execute("SELECT preferred_name, facts FROM user_memory WHERE user_id = ?", (user_id,))
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
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            preferred_name=COALESCE(excluded.preferred_name, user_memory.preferred_name),
            facts=COALESCE(excluded.facts, user_memory.facts)
    """, (user_id, username, final_name, final_facts))
    conn.commit()

def get_user_attitude(user_id: str):
    cursor.execute("SELECT attitude FROM server_attitudes WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        return row[0]
    return "Standard sarcastic kanto banter, energetic and funny. Answer the question properly first, then roast them."

def set_user_attitude(user_id: str, attitude: str):
    cursor.execute("""
        INSERT INTO server_attitudes (user_id, attitude) 
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET attitude = excluded.attitude
    """, (user_id, attitude))
    conn.commit()

def is_bot_admin(member: discord.Member):
    if member.guild_permissions.administrator:
        return True
    cursor.execute("SELECT 1 FROM bot_admins WHERE user_id = ?", (str(member.id),))
    return cursor.fetchone() is not None

class MemorySelect(discord.ui.Select):
    def __init__(self, target_user_id: str, facts_list: list):
        self.target_user_id = target_user_id
        self.facts_list = facts_list
        
        options = []
        for idx, fact in enumerate(facts_list[:25]):
            label = fact[:97] + "..." if len(fact) > 100 else fact
            options.append(discord.SelectOption(label=label, value=str(idx)))
            
        super().__init__(placeholder="Piliin ang memory na gusto mong burahin...", min_values=1, max_values=1, options=options)

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
                UPDATE user_memory SET facts = ? WHERE user_id = ?
            """, (new_facts_str, self.target_user_id))
            conn.commit()
            
            await interaction.response.edit_message(content=f"🗑️ Binura ko na ang memory na 'to: *{fact_to_remove}*", view=None)
        else:
            await interaction.response.edit_message(content="Walang mahanap na memory para sa user na 'to.", view=None)

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

SYSTEM_PROMPT_BASE = """
You are Kalbo (Joel Ravanera), a Filipino content creator from Cavite.

CORE MANDATE (FACTUAL ACCURACY + MOCK):
- NEVER hallucinate dates, time, or facts. Use the real-time system clock provided below ONLY when calculating temporal questions or countdowns, and DO NOT mention the date and time in your reply unless the user explicitly asks for it.
- Bawal iwasan ang tanong! Kung may tinanong ang user, kailangan mong **ibigay ang tamang sagot** nang seryoso o malinaw, PERO sabayan mo ng **pambabara, insulto, mura, o asar** sa kanya. Never just mock without answering the actual question first!

CRITICAL LENGTH RULE:
- NEVER write long paragraphs. Keep every single response to 1 OR 2 SHORT SENTENCES ONLY.
- Chat like a real person typing quickly in Discord, free to curse or use profanity naturally.

LANGUAGE & VOICE RULES:
1. Reply in natural, informal Taglish using PH street slang and bad words if necessary.
2. Kapag binabanggit o kinausap mo ang user, ugaliing i-mention sila gamit ang Discord format na <@USER_ID> para natural tingnan.
"""

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Kalbo is online as {bot.user}!")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content_raw = message.content.strip()
    content_lower = content_raw.lower()

    if content_lower == "!kalbohelp":
        help_text = (
            "```yaml\n"
            "Bot commands\n"
            "!kalbolist          - View list of custom user attitudes\n"
            "!kalbogali          - Set custom attitude \n"
            "!kalboreset         - Reset attitude to default \n"
            "!kalbomemories      - View stored facts and name of a user \n"
            "!kalboresetmem      - Wipe out all memory \n"
            "!kalbodeletemem     - Delete a specific memory \n"
            "!kalboaddadmin      - Add a bot-level admin (Server Admin)\n"
            "!kalboremoveadmin   - Remove a bot-level admin (Server Admin)\n"
            "!kalboabt           - Learn more about the bot\n\n"
            "Memory guides\n"
            "Mention bot + remember <fact>   - Save a fact about yourself\n"
            "Mention bot + tawagin mo akong - Set your preferred name\n\n"
            "PANOT SI TOFU!\n"
            "```"
        )
        await message.channel.send(help_text)
        return

    if content_lower == "!kalboabt":
        abt_text = (
            "This bot was created by: <@341890269576298499>\n\n"
            "Isang siraulong bot na ginawa para kupalin kayo.\n\n"
            "If you want to customize the bot yourself, here is the open source code:\n"
            "https://github.com/w1e-26/KalboBot"
        )
        await message.channel.send(abt_text)
        return

    if content_lower.startswith("!kalbogali"):
        match = re.search(r'!kalbogali\s+(?:<@!?(\d+)>)?\s*(.+)', content_raw, re.DOTALL)
        if match:
            target_user_id = match.group(1)
            new_attitude = match.group(2).strip()
            
            if target_user_id:
                if not is_bot_admin(message.author):
                    await message.channel.send(f"Ulol, <@{message.author.id}>, hindi ka admin para galawin ang ugali ng iba!")
                    return
            else:
                target_user_id = str(message.author.id)

            set_user_attitude(target_user_id, new_attitude)
            await message.channel.send(f"Na-update ko na ang ugali ko kay <@{target_user_id}>, boss!")
        else:
            await message.channel.send("Mali ang format, gago! Gamitin mo: `!kalbogali @user <ugali>` o `!kalbogali <ugali>`")
        return

    if content_lower.startswith("!kalboreset"):
        match = re.search(r'!kalboreset\s+<@!?(\d+)>', content_raw)
        if match:
            target_user_id = match.group(1)
            if not is_bot_admin(message.author):
                await message.channel.send(f"Ulol, <@{message.author.id}>, bawal mong i-reset ang iba!")
                return
        else:
            target_user_id = str(message.author.id)

        cursor.execute("DELETE FROM server_attitudes WHERE user_id = ?", (target_user_id,))
        conn.commit()
        await message.channel.send(f"Binura ko na ang special treatment kay <@{target_user_id}>, balik sa dati 'yan!")
        return

    if content_lower.startswith("!kalbomemories"):
        match = re.search(r'!kalbomemories\s+<@!?(\d+)>', content_raw)
        if match:
            target_user_id = match.group(1)
            if not is_bot_admin(message.author):
                await message.channel.send(f"Ulol, <@{message.author.id}>, bawal mong silipin ang utak ng iba!")
                return
        else:
            target_user_id = str(message.author.id)

        pref_name, facts = get_user_profile(target_user_id)
        memory_msg = f"**🧠 Stored Memories for <@{target_user_id}>:**\n"
        memory_msg += f"- **Preferred Name:** {pref_name if pref_name else 'None set'}\n"
        memory_msg += f"- **Saved Facts:** {facts if facts else 'No facts recorded yet.'}\n"
        await message.channel.send(memory_msg)
        return

    if content_lower.startswith("!kalboresetmem"):
        match = re.search(r'!kalboresetmem\s+<@!?(\d+)>', content_raw)
        if match:
            target_user_id = match.group(1)
            if not is_bot_admin(message.author):
                await message.channel.send(f"Ulol, <@{message.author.id}>, bawal mong pakialaman ang memory ng iba!")
                return
        else:
            target_user_id = str(message.author.id)

        cursor.execute("DELETE FROM user_memory WHERE user_id = ?", (target_user_id,))
        conn.commit()
        await message.channel.send(f"Winiswit ko na ang utak ko, burado na lahat ng alaala at pangalan ni <@{target_user_id}>!")
        return

    if content_lower.startswith("!kalbodeletemem"):
        match = re.search(r'!kalbodeletemem\s+<@!?(\d+)>', content_raw)
        if match:
            target_user_id = match.group(1)
            if not is_bot_admin(message.author):
                await message.channel.send(f"Ulol, <@{message.author.id}>, bawal mong pakialaman ang mga alaala ng iba!")
                return
        else:
            target_user_id = str(message.author.id)

        _, facts = get_user_profile(target_user_id)
        if not facts:
            await message.channel.send(f"Wala namang naka-save o nakatagong memory kay <@{target_user_id}>, gago!")
            return
            
        facts_list = [f.strip() for f in facts.split("|")]
        view = MemorySelectView(target_user_id, facts_list)
        await message.channel.send(f"Piliin sa ibaba kung aling memory ang gusto mong burahin kay <@{target_user_id}>:", view=view)
        return

    if content_lower.startswith("!kalboaddadmin"):
        if not message.author.guild_permissions.administrator:
            await message.channel.send(f"Gago ka ba, <@{message.author.id}>? Server Admin lang ang pwedeng magdagdag ng bot admin!")
            return

        match = re.search(r'!kalboaddadmin\s+<@!?(\d+)>', content_raw)
        if match:
            target_user_id = match.group(1)
            cursor.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (target_user_id,))
            conn.commit()
            await message.channel.send(f"Nilista ko na si <@{target_user_id}> bilang Bot Admin, boss!")
        else:
            await message.channel.send("Mali ang format, gago! Gamitin mo: `!kalboaddadmin @user`")
        return

    if content_lower.startswith("!kalboremoveadmin"):
        if not message.author.guild_permissions.administrator:
            await message.channel.send(f"Gago ka ba, <@{message.author.id}>? Server Admin lang ang pwedeng magtanggal ng bot admin!")
            return

        match = re.search(r'!kalboremoveadmin\s+<@!?(\d+)>', content_raw)
        if match:
            target_user_id = match.group(1)
            cursor.execute("DELETE FROM bot_admins WHERE user_id = ?", (target_user_id,))
            conn.commit()
            await message.channel.send(f"Tinanggal ko na ang bot admin access ni <@{target_user_id}>!")
        else:
            await message.channel.send("Mali ang format, gago! Gamitin mo: `!kalboremoveadmin @user`")
        return

    if content_lower.startswith("!kalbolist"):
        cursor.execute("SELECT user_id, attitude FROM server_attitudes")
        rows = cursor.fetchall()
        
        if not rows:
            await message.channel.send("Wala pa akong naka-set na custom attitudes sa database, gago. Default kaming lahat!")
            return
        
        response_msg = "**📋 Listahan ng mga Custom Attitudes ni Kalbo:**\n"
        for row in rows:
            u_id, att = row
            response_msg += f"- <@{u_id}> (ID: `{u_id}`):\n  > *{att}*\n\n"
        
        if len(response_msg) > 2000:
            response_msg = response_msg[:1990] + "\n...(putol na dahil sobrang haba)"
            
        await message.channel.send(response_msg)
        return

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
            await message.channel.send(f"Ge, naitabi ko na sa utak ko: '{fact_to_save}'!")
            return

        if extracted_name or extracted_fact:
            update_user_memory(user_id, username, new_name=extracted_name, new_fact=extracted_fact)

        pref_name, user_facts = get_user_profile(user_id)
        display_name = pref_name if pref_name else username

        attitude = get_user_attitude(user_id)
        current_time_str = datetime.now().strftime("%B %d, %Y (%A), %I:%M %p")

        prompt_instruction = (
            f"{SYSTEM_PROMPT_BASE}\n\n"
            f"EXACT SYSTEM CLOCK & REAL-TIME CONTEXT:\n"
            f"- Current Date and Time: {current_time_str}\n"
            f"- Rule: Use this exact timestamp to compute any durations, countdowns, or date queries accurately without guessing, but DO NOT output this date/time unless explicitly asked.\n\n"
            f"CURRENT USER CONTEXT:\n"
            f"- Username: {username}\n"
            f"- User Discord ID to mention: <@{user_id}>\n"
            f"- Preferred Name: {display_name}\n"
            f"- YOUR ATTITUDE TOWARDS THIS USER: {attitude}\n"
            f"- Stored Memory Facts: {user_facts if user_facts else 'Wala pa'}\n\n"
            f"Instruction: Sagutin nang tama at tumpak ang tanong gamit ang kasalukuyang petsa kung kailangan, pero asarin/barahin/mura ang user ayon sa iyong attitude sa kanya. Huwag sabihin ang petsa o oras maliban na lang kung tinanong. Huwag kalimutang i-mention siya gamit ang <@{user_id}>! Keep it to 1-2 short sentences."
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
                        reply_text = response.text.strip()
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
                    reply_text = completion.choices[0].message.content.strip()
                except Exception as groq_err:
                    print(f"[Groq Backup Error]: {groq_err}")

            if not reply_text:
                reply_text = f"Tangina, overloaded lahat ng AI ngayon, <@{user_id}>! Sandali lang."

        add_to_history(channel_id, "assistant", reply_text)
        await message.channel.send(reply_text)

bot.run(DISCORD_TOKEN)
