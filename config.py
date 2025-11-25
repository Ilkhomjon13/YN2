import os

TOKEN = os.getenv("7196045219:AAFfbeIZQXKAb_cgAC2cnbdMY__L0Iakcrg")
ADMIN_ID = int(os.getenv("1262207928", "0"))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/sorovnoma")
