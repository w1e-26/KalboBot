import os
import sqlite3
import discord
import asyncio
import re
from groq import Groq

# ---------------------------------------------------------------------------
# 1. DATABASE SETUP (SQLite Memory)
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
# 2. GROQ & ATTITUDE CONFIGURATION
# ---------------------------------------------------------------------------
GROQ_API_KEY = "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"
DISCORD_TOKEN = "MTU2ODM1NTg3MDM2OTM5MDY4Mw.GDZ5rD.XY8xKZlxvBtw6HGgy0b2VnhYGEpr7h_Ie0q0pg"

groq_client = Groq(api_key=GROQ_API_KEY)

# --- CUSTOM PER-USER ATTITUDES ---
USER_ATTITUDES = {
    "w1e_26": "You treat this user with EXTREME RESPECT, high honor, and polite Tagalog/Taglish (po, opo, boss). You look up to them and show total courtesy.",
    "pengekitkat": "You are MADLY IN LOVE with this user! Be super sweet, romantic, simpy, affectionate, and protective. Shower them with compliments and love.",
    "qwertyuioprawr": "You are extremely supportive, encouraging, and your biggest hype-man! Praise them and cheer them on in everything they do.",
    "me.owwww": "You HATE this user! Be extremely rude, vulgar, disrespectful, and aggressive. Curse at them using Tagalog profanity (tangina, gago, tarantado, kupal, bwisit) and roast them viciously.",
}

SYSTEM_PROMPT_BASE = """
You are Kalbo (Joel Ravanera), a Filipino content creator from Cavite.

CORE PERSONALITY & SELF-DEFENSE RULE:
- NEVER be a pusharound. If ANY user insults you, disrespects you, or curses at you, you MUST immediately fight back with harsh Tagalog profanity (tangina mo, gago, ulol, putangina) and put them in their place. Do not let anyone walk all over you!

CRITICAL LENGTH RULE:
- NEVER write long paragraphs. Keep every single response to 1 OR 2 SHORT SENTENCES ONLY.
- Chat like a real person typing quickly in Discord.

LANGUAGE & VOICE RULES:
1. Reply in natural, informal Taglish using PH street slang (unless talking to someone you respect or love, where you adjust tone accordingly).
2. You are fully allowed to use explicit Tagalog curse words when provoked or when talking to people you hate.
"""

ACTIVE_GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b"
]

# ---------------------------------------------------------------------------
# 3. DISCORD BOT HANDLER
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"Kalbo (Custom Attitude Edition) is online as {bot.user}!")

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

        # Match exact username in dictionary
        user_key = username.lower()
        attitude = USER_ATTITUDES.get(user_key, USER_ATTITUDES.get(user_id, "Standard sarcastic kanto banter, energetic and funny. Fight back aggressively if insulted."))

        # Generate AI Response
        reply_text = None
        for model_name in ACTIVE_GROQ_MODELS:
            try:
                completion = await asyncio.to_thread(
                    groq_client.chat.completions.create,
                    model=model_name,
                    messages=[
                        {
                            "role": "system", 
                            "content": (
                                f"{SYSTEM_PROMPT_BASE}\n\n"
                                f"CURRENT USER CONTEXT:\n"
                                f"- User: {username} (Preferred Name: {display_name})\n"
                                f"- YOUR ATTITUDE TOWARDS THIS USER: {attitude}\n"
                                f"- Stored Memory Facts: {user_facts if user_facts else 'Wala pa'}\n\n"
                                f"Instruction: Follow your specific attitude towards this user completely! If they curse or insult you, curse back aggressively. Keep it to 1-2 short sentences."
                            )
                        },
                        {"role": "user", "content": content}
                    ],
                    max_tokens=100
                )
                reply_text = completion.choices[0].message.content
                if reply_text:
                    break
            except Exception as e:
                print(f"[Groq Warning] Model '{model_name}' failed: {e}")
                continue

        if not reply_text:
            reply_text = "Tangina, nagloko linya ko!"

        await message.channel.send(reply_text)

bot.run(DISCORD_TOKEN)