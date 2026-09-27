from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, Column, Integer, String, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel
from typing import List, Optional
import os

# ==========================================
# 1. НАСТРОЙКА БАЗЫ ДАННЫХ (SQLite)
# ==========================================
DATABASE_URL = "sqlite:///./database.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Таблица Курсов
class CourseModel(Base):
    __tablename__ = "courses"

    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    color = Column(String)
    lectures = Column(JSON)   # Храним слоты и расписание лекций как JSON
    practices = Column(JSON)  # Храним слоты и расписание практик как JSON

# Таблица Академического прогресса (Транскрипт)
class TranscriptModel(Base):
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String)
    name = Column(String)
    credits = Column(Integer)
    status = Column(String) # 'completed' или 'in-progress'

# Создание таблиц в базе данных
Base.metadata.create_all(bind=engine)

# Зависимость для получения сессии БД
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# 2. СХЕМЫ ДАННЫХ (Pydantic)
# ==========================================
class CourseSchema(BaseModel):
    id: str
    name: str
    color: str
    lectures: List[dict]
    practices: List[dict]

    class Config:
        from_attributes = True

class TranscriptSchema(BaseModel):
    code: str
    name: str
    credits: int
    status: str

    class Config:
        from_attributes = True


# ==========================================
# 3. FASTAPI ПРИЛОЖЕНИЕ И ЭНДПОИНТЫ
# ==========================================
app = FastAPI(title="Schedule & Degree Audit API")

# --- Эндпоинты для Курсов (Schedule Builder) ---

@app.get("/api/courses", response_model=List[CourseSchema])
def get_courses(db: Session = Depends(get_db)):
    """Получить все сохраненные курсы"""
    return db.query(CourseModel).all()

@app.post("/api/courses", response_model=List[CourseSchema])
def save_courses(courses: List[CourseSchema], db: Session = Depends(get_db)):
    """Перезаписать/сохранить список курсов"""
    db.query(CourseModel).delete() # Простая перезапись для демонстрации
    for c in courses:
        db_course = CourseModel(
            id=c.id,
            name=c.name,
            color=c.color,
            lectures=c.lectures,
            practices=c.practices
        )
        db.add(db_course)
    db.commit()
    return db.query(CourseModel).all()

# --- Эндпоинты для Транскрипта (Degree Progress) ---

@app.get("/api/transcript", response_model=List[TranscriptSchema])
def get_transcript(db: Session = Depends(get_db)):
    """Получить историю успеваемости из БД"""
    items = db.query(TranscriptModel).all()
    
    # Если база пустая, забиваем дефолтными данными для демонстрации
    if not items:
        default_items = [
            TranscriptModel(code="MATH101", name="Calculus I", credits=5, status="completed"),
            TranscriptModel(code="ENG101", name="Academic Writing", credits=4, status="completed"),
            TranscriptModel(code="CS101", name="Intro to Programming", credits=5, status="in-progress")
        ]
        db.add_all(default_items)
        db.commit()
        items = db.query(TranscriptModel).all()
        
    return items


# ==========================================
# 4. РАЗДАЧА СТАТИКИ (HTML-файла)
# ==========================================
if os.path.exists("public"):
    app.mount("/static", StaticFiles(directory="public"), name="static")

@app.get("/")
def read_index():
    """Отдает страницу сайта при переходе на http://127.0.0.1:8000/"""
    return FileResponse("public/schedule-builder.html")