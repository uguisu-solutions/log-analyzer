"""SQLAlchemy モデル（解析サンプル用）。orders テーブルを定義。"""
from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    payment_id = Column(Integer, ForeignKey("payments.id"))
    note = Column(String(255))
