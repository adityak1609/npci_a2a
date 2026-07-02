import re
import os

# 1. Read main.py
with open('main.py', 'r', encoding='utf-8') as f:
    main_code = f.read()

# 2. Extract helpers
helpers_to_extract = [
    '_generate_content_once', 
    '_generate_with_retry', 
    '_verified_answer_chunks', 
    '_get_or_create_session', 
    '_load_chat_history', 
    '_allowed_visibility_tiers', 
    '_save_messages', 
    '_build_gemini_contents', 
    'format_utc_datetime'
]

extracted_code = ""

for func in helpers_to_extract:
    # Match def func(...) ... until next def or end
    pattern = rf'^(async )?def {func}\(.*?(?=^(async )?def |\Z)'
    match = re.search(pattern, main_code, flags=re.MULTILINE | re.DOTALL)
    if match:
        extracted_code += match.group(0) + '\n\n'
        main_code = main_code.replace(match.group(0), '')
    else:
        print(f"Could not find {func}")

# Add import format_utc_datetime to main.py
main_code = main_code.replace('from auth import User', 'from auth import User\nfrom utils import format_utc_datetime')

with open('utils.py', 'w', encoding='utf-8') as f:
    f.write('''import asyncio
import os
from typing import Optional
from sqlalchemy.orm import Session
from auth import User
from database import ChatSession, ChatMessage
from guardrails import sanitize_question
from config import GEMINI_MODEL, VERIFIED_STREAM_CHUNK_SIZE, CHAT_MEMORY_SIZE, MAX_RETRIES
import time
from telemetry import _log_internal_error
from metrics import time_request
from metrics import observe_histogram
from compliance import require_external_ai_approval
import logging
logger = logging.getLogger(__name__)
_RETRYABLE = ["429", "503", "500", "ReadTimeout"]
from agent.dispatcher import client
from datetime import timezone

''')
    f.write(extracted_code)

# 3. Write back main.py
with open('main.py', 'w', encoding='utf-8') as f:
    f.write(main_code)

# 4. Update routes/chat_routes.py
with open('routes/chat_routes.py', 'r', encoding='utf-8') as f:
    chat_code = f.read()

chat_code = chat_code.replace(
    'from utils import format_utc_datetime', 
    'from utils import format_utc_datetime, _allowed_visibility_tiers, _build_gemini_contents, _generate_with_retry, _load_chat_history, _save_messages, _verified_answer_chunks, _get_or_create_session'
)
chat_code = chat_code.replace(
    'import asyncio\n', 
    'import asyncio\nfrom prompt import build_prompt\nfrom retriever import retrieve, retrieve_with_scores\n'
)

with open('routes/chat_routes.py', 'w', encoding='utf-8') as f:
    f.write(chat_code)

# 5. Update routes/admin_routes.py
with open('routes/admin_routes.py', 'r', encoding='utf-8') as f:
    admin_code = f.read()

admin_imports = '''
import time
from metrics import observe_histogram
from compliance import ComplianceBlockedError
from utils import format_utc_datetime, _generate_content_once
from telemetry import _log_internal_error
from database import DocumentSummary
from a2a_audit import _write_audit_log
'''
admin_code = admin_code.replace('import json\n', 'import json\n' + admin_imports)

with open('routes/admin_routes.py', 'w', encoding='utf-8') as f:
    f.write(admin_code)

print("Refactoring done.")
