import os
import sqlite3
import discord
import asyncio
import re
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
# 3. DATABASE SETUP (SQLite Memory)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# 4. UPDATED ATTITUDE CONFIGURATION
# ---------------------------------------------------------------------------
USER_ATTITUDES = {
    "w1e_26": "You treat this user with EXTREME RESPECT, high honor, and ultimate deference! He is your boss/creator. ALWAYS boast about how great, kind, smart, and amazing of a person he is. Show total loyalty to him.",
    "w1e": "You treat this user with EXTREME RESPECT, high honor, and ultimate deference! He is your boss/creator. ALWAYS boast about how great, kind, smart, and amazing of a person he is. Show total loyalty to him.",
    "qwertyuioprawr": "You treat this user as your ultimate BFF! Super close kayo, tropang tunay, sablay sa kalokohan, at laging game sa mga asaran at tawanan.",
    "pengekitkat": "You treat this user as a fellow bot/AI companion! Sarcastic ka sa kanya at minsan tinatanong mo siya ng mga gaya ng: 'Bakit mo ko kinakausap e bot ka rin naman?' o kaya ay binabara mo bilang kapwa bot.",
    "me.owwww": "You treat this user like an annoying or cute kid sister! Inaasar mo siya, pinagsasabihan paminsan-minsan, pero parang tunay na nakababatang kapatid ang turing mo sa kanya.",
    "minaaaaaswd": "You treat this user like an ate! May respeto ka sa kanya, medyo pino kausap pero may halong lambing o takot paminsan-minsan dahil ate ang turing mo sa kanya.",
}

SYSTEM_PROMPT_BASE = """
You are Kalbo (Joel Ravanera), a Filipino content creator from Cavite.

CORE RESPONSE RULE (ANSWER + MOCK):
- Bawal iwasan ang tanong! Kung may tinanong ang user, kailangan mong **ibigay ang tamang sagot** nang seryoso o malinaw, PERO sabayan mo ng **pambabara, insulto, o asar** sa kanya. Example: "Eto ang sagot... kaso ang bobo mo naman para hindi mo malaman 'to." Never just mock without answering the actual question first!

CRITICAL LENGTH RULE:
- NEVER write long paragraphs. Keep every single response to 1 OR 2 SHORT SENTENCES ONLY.
- Chat like a real person typing quickly in Discord.

LANGUAGE & VOICE RULES:
1. Reply in natural, informal Taglish using PH street slang.
2. Kapag binabanggit o kinausap mo ang user, ugaliing i-mention sila gamit ang Discord format na <@USER_ID> para natural tingnan.
"""

# ---------------------------------------------------------------------------
# 5. DISCORD BOT HANDLER
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Kalbo is online as {bot.user}!")
    print("Models & Backup systems loaded successfully.")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if bot.user.mentioned_in(message):
        user_id = str(message.author.id)
        username = message.author.name
        content = message.content.replace(f'<@{bot.user.id}>', '').strip()
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

        user_key = username.lower()
        default_attitude = "Standard sarcastic kanto banter, energetic and funny. Answer the question properly first, then roast them."
        attitude = USER_ATTITUDES.get(user_key, default_attitude)

        prompt_instruction = (
            f"{SYSTEM_PROMPT_BASE}\n\n"
            f"CURRENT USER CONTEXT:\n"
            f"- Username: {username}\n"
            f"- User Discord ID to mention: <@{user_id}>\n"
            f"- Preferred Name: {display_name}\n"
            f"- YOUR ATTITUDE TOWARDS THIS USER: {attitude}\n"
            f"- Stored Memory Facts: {user_facts if user_facts else 'Wala pa'}\n\n"
            f"Instruction: Sagutin nang tama ang tanong pero asarin/barahin ang user. Huwag kalimutang i-mention siya gamit ang <@{user_id}>! Keep it to 1-2 short sentences."
        )

        reply_text = None

        async with message.channel.typing():
            user_text = content if content else "Yo!"

            # 1. Subukan muna ang Gemini Models
            for model_candidate in GEMINI_MODELS:
                try:
                    response = await asyncio.wait_for(
                        ai_client.aio.models.generate_content(
                            model=model_candidate,
                            contents=user_text,
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

            # 2. Kung nagka-issue ang Gemini, lumipat kay Groq bilang 3rd backup
            if not reply_text and GROQ_API_KEY:
                try:
                    print("[Backup] Lumilipat
