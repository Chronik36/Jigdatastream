from jigstream import db, tracking
from jigstream.app import create_app

if __name__ == "__main__":
    tracking.start_background_refresh(db.connect)
    create_app().run(host="127.0.0.1", port=5000, debug=False)
