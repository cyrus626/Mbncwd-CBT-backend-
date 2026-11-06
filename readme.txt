run this codes inside cmd bash

pip install fastapi "sqlalchemy>=2" pyjwt "pwdlib[argon2]"
pip install "pydantic[email]" python-multipart uvicorn
pip install openpyxl
pip install reportlab
pip install -r requirements.txt

create a SECRET CODE with this and copy into .env.example then later rename to .env
python -c "import secrets; print(secrets.token_urlsafe(48))"

Run python create_admin.py, then uvicorn main:app --reload