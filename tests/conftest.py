import os
import sys
from pathlib import Path

# manaba_bot は import した時点で .env / 環境変数を読むので、先にテスト用の値を入れておく
os.environ.update(MANABA_USER="u1", MANABA_PASSWORD="pw", DISCORD_TOKEN="dummy",
                  DISCORD_CHANNEL_ID="1", MANABA_BASE_URL="http://127.0.0.1:1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
