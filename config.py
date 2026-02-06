import os

class Config:
    # Database
    # Format: postgresql://user:password@host:port/dbname
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///" + os.path.join(os.getcwd(), "atp_dev.db"))

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Payments
    # The Ethereum address where platform commissions (20%) will be settled
    ADMIN_WALLET_ADDRESS = os.getenv("ADMIN_WALLET_ADDRESS", "0x8396e3ebf85d0d400045965f427d6bb5a12137b3")


    # Security
    SECRET_KEY = os.getenv("SECRET_KEY", "cyberpunk-secret-88k")
