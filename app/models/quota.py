from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typing import Optional


class Base(DeclarativeBase):
    pass


class Quota(Base):
    """Read/write mapping of the existing quotas table; no schema changes."""

    __tablename__ = "quotas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quota_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    quota_type: Mapped[str] = mapped_column(Text, nullable=False)
    approval_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quota_serial: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    person_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_date: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expiry_date: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False)
    replacement_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_replacement_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    is_deleted: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    def as_dict(self):
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}
