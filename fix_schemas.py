import os

with open('utils.py', 'r', encoding='utf-8') as f:
    utils_lines = f.readlines()

schemas_and_routers = utils_lines[168:208]

utils_lines = utils_lines[:168] + utils_lines[208:]
with open('utils.py', 'w', encoding='utf-8') as f:
    f.writelines(utils_lines)

# separate schemas and routers
schemas = schemas_and_routers[:23]
routers = schemas_and_routers[23:]

# Append schemas to routes/chat_routes.py
with open('routes/chat_routes.py', 'r', encoding='utf-8') as f:
    chat_content = f.read()

# Make sure BaseModel is imported in chat_routes.py
if 'from pydantic import BaseModel' not in chat_content:
    chat_content = chat_content.replace('import json', 'import json\nfrom pydantic import BaseModel')

with open('routes/chat_routes.py', 'w', encoding='utf-8') as f:
    f.write(''.join(schemas) + '\n' + chat_content)

# Append routers to main.py
with open('main.py', 'r', encoding='utf-8') as f:
    main_content = f.read()

main_content = main_content.replace('def _frontend_response', ''.join(routers) + '\n\ndef _frontend_response')

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(main_content)
