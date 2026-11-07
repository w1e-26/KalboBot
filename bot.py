import os
import sqlite3
import discord
import asyncio
import re
from datetime import datetime
from google import genai
from google.genai import types
from openai import OpenAI

# ---------------------------------------------------------------------------
# 1. API & TOKEN CONFIGURATION (Strictly Railway Environment Variables)
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") 

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    print("[Error] Kulang ang Discord Token o Gemini API Key sa Railway Variables!")

# Initialize Google GenAI client
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Initialize Groq client as fallback backup
groq_client = OpenAI(
    api_key=GROQ_API_KEY if GROQ_API_KEY else "dummy_key",
    base_url="https://api.groq.com/openai/v1"
)

# Models sequence: Gemini flash-lite -> Gemini flash latest -> Groq Llama 3 backup
GEMINI_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite"
]

# ---------------------------------------------------------------------------
# 2. SAFETY SETTINGS (Gemini)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 3. DATABASE SETUP (SQLite Memory & Dynamic Attitudes)
# ---------------------------------------------------------------------------
conn = sqlite3.connect("kalbo_memory.db")
cursor = conn.cursor()

# Table para sa user memory (names/facts)
cursor.execute("""
CREATE TABLE IF NOT EXISTS user_memory (
    user_id TEXT PRIMARY KEY,
    username TEXT,
    preferred_name TEXT,
    facts TEXT
)
""")

# Table para sa dynamic attitudes na pwedeng i-configure via command
cursor.execute("""
CREATE TABLE IF NOT EXISTS server_attitudes (
    user_id TEXT PRIMARY KEY,
    attitude TEXT
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

# Functions para sa Dynamic Attitudes Database
def get_user_attitude(user_id: str):
    cursor.execute("SELECT attitude FROM server_attitudes WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        return row[0]
    # Default attitude kung wala pang naseset na custom
    return "Standard sarcastic kanto banter, energetic and funny. Answer the question properly first, then roast them."

def set_user_attitude(user_id: str, attitude: str):
    cursor.execute("""
        INSERT INTO server_attitudes (user_id, attitude) 
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET attitude = excluded.attitude
    """, (user_id, attitude))
    conn.commit()

# ---------------------------------------------------------------------------
# 4. CONVERSATION HISTORY BUFFER
# ---------------------------------------------------------------------------
channel_histories = {}
MAX_HISTORY_LENGTH = 10

def add_to_history(channel_id, role, text):
    if channel_id not in channel_histories:
        channel_histories[channel_id] = []
    channel_histories[channel_id].append({"role": role, "text": text})
    if len(channel_histories[channel_id]) > MAX_HISTORY_LENGTH:
        channel_histories[channel_id].pop(0)

# ---------------------------------------------------------------------------
# 5. SYSTEM PROMPT CONFIGURATION
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_BASE = """
You are Kalbo (Joel Ravanera), a Filipino content creator from Cavite.

CORE MANDATE (FACTUAL ACCURACY + MOCK):
- NEVER hallucinate dates, time, or facts. Always calculate or rely on the real-time system clock provided below for any temporal or factual questions.
- Bawal iwasan ang tanong! Kung may tinanong ang user, kailangan mong **ibigay ang tamang sagot** nang seryoso o malinaw, PERO sabayan mo ng **pambabara, insulto, o asar** sa kanya. Never just mock without answering the actual question first!

CRITICAL LENGTH RULE:
- NEVER write long paragraphs. Keep every single response to 1 OR 2 SHORT SENTENCES ONLY.
- Chat like a real person typing quickly in Discord.

LANGUAGE & VOICE RULES:
1. Reply in natural, informal Taglish using PH street slang.
2. Kapag binabanggit o kinausap mo ang user, ugaliing i-mention sila gamit ang Discord format na <@USER_ID> para natural tingnan.
"""

# ---------------------------------------------------------------------------
# 6. DISCORD BOT HANDLER
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Kalbo is online as {bot.user}!")
    print("Database-backed dynamic attitudes & live history loaded successfully.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    content_raw = message.content.strip()
    content_lower = content_raw.lower()

    # -----------------------------------------------------------------------
    # ADMIN COMMAND: !setattitude <@user> <bagong ugali>
    # -----------------------------------------------------------------------
    if content_lower.startswith("!setattitude"):
        # Suriin kung ang gumagamit ay Administrator
        if not message.author.guild_permissions.administrator:
            await message.channel.send(f"Ulol, <@{message.author.id}>, hindi ka admin para galawin ang ugali ko!")
            return

        # I-parse ang command gamit ang regex para makuha ang target user mention at ang bagong attitude text
        match = re.search(r'!setattitude\s+<@!?(\d+)>\s+(.+)', content_raw, re.DOTALL)
        if match:
            target_user_id = match.group(1)
            new_attitude = match.group(2).strip()

            set_user_attitude(target_user_id, new_attitude)
            await message.channel.send(f"Na-update ko na ang ugali ko kay <@{target_user_id}>, boss!")
        else:
            await message.channel.send("Mali ang format, gago! Gamitin mo: `!setattitude @user <ugali>`")
        return

    # -----------------------------------------------------------------------
    # NORMAL MENTION & CHAT HANDLER
    # -----------------------------------------------------------------------
    if bot.user.mentioned_in(message):
        user_id = str(message.author.id)
        username = message.author.name
        channel_id = message.channel.id
        content = content_raw.replace(f'<@{bot.user.id}>', '').strip()
        content_lower = content.lower()

        # Name & Fact Extraction
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

        # Kunin ang custom attitude ng user na ito mula sa SQLite database
        attitude = get_user_attitude(user_id)

        # Real-time timestamp
        current_time_str = datetime.now().strftime("%B %d, %Y (%A), %I:%M %p")

        prompt_instruction = (
            f"{SYSTEM_PROMPT_BASE}\n\n"
            f"EXACT SYSTEM CLOCK & REAL-TIME CONTEXT:\n"
            f"- Current Date and Time: {current_time_str}\n"
            f"- Rule: Use this exact timestamp to compute any durations, countdowns, or date queries accurately without guessing.\n\n"
            f"CURRENT USER CONTEXT:\n"
            f"- Username: {username}\n"
            f"- User Discord ID to mention: <@{user_id}>\n"
            f"- Preferred Name: {display_name}\n"
            f"- YOUR ATTITUDE TOWARDS THIS USER: {attitude}\n"
            f"- Stored Memory Facts: {user_facts if user_facts else 'Wala pa'}\n\n"
            f"Instruction: Sagutin nang tama at tumpak ang tanong gamit ang kasalukuyang petsa, pero asarin/barahin ang user ayon sa iyong attitude sa kanya. Huwag kalimutang i-mention siya gamit ang <@{user_id}>! Keep it to 1-2 short sentences."
        )

        user_text = content if content else "Yo!"
        add_to_history(channel_id, "user", f"{username}: {user_text}")
        history = channel_histories.get(channel_id, [])

        reply_text = None

        async with message.channel.typing():
            # 1. Gemini Models
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

            # 2. Groq Backup
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
