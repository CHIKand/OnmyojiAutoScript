from pydantic import BaseModel


class OrchestraRow(BaseModel):
    """编排表的一行，即一轮。task 之外的字段为 None 表示未填。"""
    task: str
    sub1: str | None = None
    sub2: str | None = None
    max_count: int | None = None
    max_minutes: int | None = None
    rest_minutes: int | None = None
