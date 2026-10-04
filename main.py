from email.message import EmailMessage
import hashlib
import hmac
import math
import os
import re
import secrets
import smtplib
import time
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, ForeignKey, Integer, JSON, String, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base, sessionmaker


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./database.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# Legacy table retained so existing local data is not destroyed.
class CourseModel(Base):
    __tablename__ = "courses"

    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    color = Column(String)
    lectures = Column(JSON)
    practices = Column(JSON)


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False)
    email = Column(String(254), unique=True, index=True, nullable=False)
    password_hash = Column(String(512), nullable=False)
    created_at = Column(Integer, nullable=False)


class AuthSessionModel(Base):
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    expires_at = Column(Integer, index=True, nullable=False)


class PasswordResetModel(Base):
    __tablename__ = "password_resets"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    expires_at = Column(Integer, index=True, nullable=False)
    used_at = Column(Integer, nullable=True)


class SavedCourseModel(Base):
    __tablename__ = "user_courses"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    course_uid = Column(String(100), nullable=False)
    course_code = Column(String(40), nullable=True)
    name = Column(String(200), nullable=False)
    color = Column(String(32), nullable=False)
    lectures = Column(JSON, nullable=False)
    practices = Column(JSON, nullable=False)


class CurriculumModel(Base):
    __tablename__ = "curricula"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    name = Column(String(120), nullable=False)
    categories = Column(JSON, nullable=False)
    progress = Column(JSON, nullable=True)
    updated_at = Column(Integer, nullable=False)


class TranscriptModel(Base):
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String)
    name = Column(String)
    credits = Column(Integer)
    status = Column(String)


Base.metadata.create_all(bind=engine)


def ensure_curriculum_progress_column() -> None:
    """Add the progress column for databases created by an earlier app version."""
    if engine.dialect.name != "sqlite":
        return
    columns = {column["name"] for column in inspect(engine).get_columns("curricula")}
    if "progress" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE curricula ADD COLUMN progress JSON"))


ensure_curriculum_progress_column()


def ensure_user_course_code_column() -> None:
    """Keep existing saved schedules compatible with prerequisite checks."""
    if engine.dialect.name != "sqlite":
        return
    columns = {column["name"] for column in inspect(engine).get_columns("user_courses")}
    if "course_code" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE user_courses ADD COLUMN course_code VARCHAR(40)"))


ensure_user_course_code_column()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CourseSchema(BaseModel):
    id: str
    code: Optional[str] = None
    name: str
    color: str
    lectures: List[dict]
    practices: List[dict]

    model_config = ConfigDict(from_attributes=True)


class TranscriptSchema(BaseModel):
    code: str
    name: str
    credits: int
    status: str

    model_config = ConfigDict(from_attributes=True)


class RegisterSchema(BaseModel):
    name: str
    email: str
    password: str


class LoginSchema(BaseModel):
    email: str
    password: str


class ForgotPasswordSchema(BaseModel):
    email: str


class ResetPasswordSchema(BaseModel):
    token: str
    password: str


class CurriculumCourseSchema(BaseModel):
    code: str
    name: str
    credits: float
    kind: str = "required"
    semester: Optional[int] = None
    prerequisites: List[dict] = Field(default_factory=list)
    equivalents: List[dict] = Field(default_factory=list)
    eligible_slots: List[str] = Field(default_factory=list)
    elective_type: Optional[str] = None
    grade: Optional[str] = None
    completion_status: Optional[str] = None
    completed: bool = False
    in_progress: bool = False
    system_status: Optional[str] = None
    selected_in_curriculum: Optional[bool] = None
    selected_semester: Optional[int] = None


class CurriculumCategorySchema(BaseModel):
    category: str
    required: List[CurriculumCourseSchema]


class CurriculumSchema(BaseModel):
    name: str
    categories: List[CurriculumCategorySchema]
    progress: Optional[dict] = None


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_ITERATIONS = 390_000
SESSION_TTL_SECONDS = int(os.getenv("AUTH_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60)))
RESET_TTL_SECONDS = int(os.getenv("AUTH_RESET_TTL_SECONDS", str(30 * 60)))
COOKIE_NAME = "schedule_session"
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
ENVIRONMENT = os.getenv("APP_ENV", "development").lower()


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise HTTPException(status_code=422, detail="Введите корректный адрес электронной почты.")
    return email


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Пароль должен содержать не менее 8 символов.")
    if len(password) > 128:
        raise HTTPException(status_code=422, detail="Пароль слишком длинный.")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (TypeError, ValueError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_user(user: UserModel) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email}


def normalize_curriculum(payload: CurriculumSchema) -> tuple[str, list[dict], Optional[dict]]:
    curriculum_name = " ".join(payload.name.strip().split())
    if not curriculum_name or len(curriculum_name) > 120:
        raise HTTPException(status_code=422, detail="Название учебного плана должно содержать до 120 символов.")
    if not payload.categories or len(payload.categories) > 100:
        raise HTTPException(status_code=422, detail="Учебный план должен содержать от 1 до 100 разделов.")

    normalized_categories = []
    seen_codes = set()
    total_courses = 0
    for category in payload.categories:
        category_name = " ".join(category.category.strip().split())
        if not category_name or len(category_name) > 120:
            raise HTTPException(status_code=422, detail="Проверьте названия разделов учебного плана.")
        if not category.required:
            raise HTTPException(
                status_code=422, detail=f'Раздел "{category_name}" не содержит дисциплин.'
            )

        courses = []
        for course in category.required:
            code = course.code.strip().upper()
            course_name = " ".join(course.name.strip().split())
            credits = float(course.credits)
            kind = course.kind.strip().lower()
            if not code or len(code) > 40:
                raise HTTPException(status_code=422, detail="Код дисциплины обязателен и не длиннее 40 символов.")
            if not course_name or len(course_name) > 200:
                raise HTTPException(status_code=422, detail=f"Проверьте название дисциплины {code}.")
            if not math.isfinite(credits) or credits < 0 or credits > 60:
                raise HTTPException(status_code=422, detail=f"Некорректное количество кредитов у {code}.")
            if kind not in {"required", "elective", "requisite"}:
                raise HTTPException(status_code=422, detail=f"Некорректный тип дисциплины {code}.")
            if code in seen_codes:
                raise HTTPException(status_code=422, detail=f"Код дисциплины {code} встречается несколько раз.")
            seen_codes.add(code)
            total_courses += 1

            semester = course.semester
            if semester is not None and (semester < 1 or semester > 20):
                raise HTTPException(status_code=422, detail=f"Некорректный семестр у {code}.")

            prerequisites = []
            for prerequisite in course.prerequisites[:25]:
                prerequisite_code = str(prerequisite.get("code", "")).strip().upper()[:40]
                prerequisite_name = " ".join(
                    str(prerequisite.get("name", "")).strip().split()
                )[:200]
                if not prerequisite_code and not prerequisite_name:
                    continue
                prerequisites.append(
                    {
                        "code": prerequisite_code,
                        "name": prerequisite_name,
                        "confidence": str(prerequisite.get("confidence", "")).strip()[:30],
                        "reason": " ".join(
                            str(prerequisite.get("reason", "")).strip().split()
                        )[:500],
                        "official": prerequisite.get("official")
                        if isinstance(prerequisite.get("official"), bool)
                        else None,
                    }
                )
            equivalents = []
            for equivalent in course.equivalents[:10]:
                equivalent_code = str(equivalent.get("code", "")).strip().upper()[:40]
                equivalent_name = " ".join(
                    str(equivalent.get("name", "")).strip().split()
                )[:200]
                if equivalent_code or equivalent_name:
                    equivalents.append({"code": equivalent_code, "name": equivalent_name})
            eligible_slots = [
                " ".join(str(slot).strip().split())[:200]
                for slot in course.eligible_slots[:25]
                if str(slot).strip()
            ]
            elective_type = (
                " ".join(course.elective_type.strip().split())[:80]
                if course.elective_type
                else None
            )
            grade = course.grade.strip()[:20] if course.grade else None
            completion_status = (
                course.completion_status.strip().lower().replace("-", "_")
                if course.completion_status
                else None
            )
            allowed_statuses = {
                "completed",
                "in_progress",
                "available",
                "planned",
                "not_selected",
                "not_tracked_in_curriculum",
            }
            if completion_status and completion_status not in allowed_statuses:
                raise HTTPException(
                    status_code=422, detail=f"Неизвестный статус прохождения у {code}."
                )
            completed = bool(course.completed or completion_status == "completed")
            in_progress = bool(course.in_progress or completion_status == "in_progress")
            if completed:
                completion_status = "completed"
                in_progress = False
            elif in_progress:
                completion_status = "in_progress"
            system_status = (
                " ".join(course.system_status.strip().split())[:80]
                if course.system_status
                else None
            )
            selected_in_curriculum = course.selected_in_curriculum
            if kind == "elective" and (completed or in_progress):
                selected_in_curriculum = True
            selected_semester = course.selected_semester
            if selected_semester is not None and (
                selected_semester < 1 or selected_semester > 20
            ):
                raise HTTPException(
                    status_code=422, detail=f"Некорректный выбранный семестр у {code}."
                )
            courses.append(
                {
                    "code": code,
                    "name": course_name,
                    "credits": int(credits) if credits.is_integer() else round(credits, 2),
                    "kind": kind,
                    "semester": semester,
                    "prerequisites": prerequisites,
                    "equivalents": equivalents,
                    "eligible_slots": eligible_slots,
                    "elective_type": elective_type,
                    "grade": grade,
                    "completion_status": completion_status,
                    "completed": completed,
                    "in_progress": in_progress,
                    "system_status": system_status,
                    "selected_in_curriculum": selected_in_curriculum,
                    "selected_semester": selected_semester,
                }
            )
        normalized_categories.append({"category": category_name, "required": courses})

    if total_courses > 1000:
        raise HTTPException(status_code=422, detail="Учебный план не может содержать более 1000 дисциплин.")

    normalized_progress = None
    if payload.progress is not None:
        integer_fields = [
            "total_positions",
            "completed_positions",
            "in_progress_positions",
            "available_positions",
            "planned_positions",
        ]
        number_fields = [
            "total_ects",
            "completed_ects",
            "in_progress_ects",
            "available_ects",
            "planned_ects",
        ]
        normalized_progress = {}
        try:
            for field_name in integer_fields:
                value = int(payload.progress.get(field_name, 0))
                if value < 0:
                    raise ValueError
                normalized_progress[field_name] = value
            for field_name in number_fields:
                value = float(payload.progress.get(field_name, 0))
                if not math.isfinite(value) or value < 0:
                    raise ValueError
                normalized_progress[field_name] = (
                    int(value) if value.is_integer() else round(value, 2)
                )
            percent = float(payload.progress.get("completion_percent_by_ects", 0))
            if not math.isfinite(percent) or percent < 0 or percent > 1:
                raise ValueError
            normalized_progress["completion_percent_by_ects"] = percent
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Некорректная сводка curriculum_progress.")

    return curriculum_name, normalized_categories, normalized_progress


def public_curriculum(curriculum: CurriculumModel) -> dict:
    return {
        "name": curriculum.name,
        "categories": curriculum.categories,
        "progress": curriculum.progress,
        "updated_at": curriculum.updated_at,
    }


def create_session(db: Session, user: UserModel, response: Response) -> None:
    raw_token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.query(AuthSessionModel).filter(AuthSessionModel.expires_at <= now).delete(
        synchronize_session=False
    )
    db.add(
        AuthSessionModel(
            token_hash=token_digest(raw_token),
            user_id=user.id,
            expires_at=now + SESSION_TTL_SECONDS,
        )
    )
    db.commit()
    response.set_cookie(
        key=COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> UserModel:
    raw_token = request.cookies.get(COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход.")

    now = int(time.time())
    auth_session = (
        db.query(AuthSessionModel)
        .filter(
            AuthSessionModel.token_hash == token_digest(raw_token),
            AuthSessionModel.expires_at > now,
        )
        .first()
    )
    if not auth_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия истекла.")

    user = db.query(UserModel).filter(UserModel.id == auth_session.user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пользователь не найден.")
    return user


def send_reset_email(recipient: str, reset_url: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not smtp_host:
        if ENVIRONMENT == "production":
            raise RuntimeError("SMTP_HOST must be configured in production")
        # Local fallback: the reset link is logged and exposed only outside production.
        print(f"[development] Password reset link for {recipient}: {reset_url}")
        return False

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_username).strip()
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    if not smtp_from:
        raise RuntimeError("SMTP_FROM or SMTP_USERNAME must be configured")

    message = EmailMessage()
    message["Subject"] = "Восстановление пароля — Schedule Builder"
    message["From"] = smtp_from
    message["To"] = recipient
    message.set_content(
        "Вы запросили восстановление пароля в Schedule Builder.\n\n"
        f"Откройте ссылку в течение 30 минут:\n{reset_url}\n\n"
        "Если это были не вы, просто проигнорируйте письмо."
    )

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=15) as smtp:
        if not use_ssl and use_tls:
            smtp.starttls()
        if smtp_username:
            smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)
    return True


app = FastAPI(title="Schedule & Degree Audit API")


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterSchema, response: Response, db: Session = Depends(get_db)):
    name = " ".join(payload.name.strip().split())
    if len(name) < 2 or len(name) > 80:
        raise HTTPException(status_code=422, detail="Имя должно содержать от 2 до 80 символов.")
    email = normalize_email(payload.email)
    validate_password(payload.password)

    user = UserModel(
        name=name,
        email=email,
        password_hash=hash_password(payload.password),
        created_at=int(time.time()),
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Аккаунт с такой почтой уже существует.")

    create_session(db, user, response)
    return {"user": public_user(user)}


@app.post("/api/auth/login")
def login(payload: LoginSchema, response: Response, db: Session = Depends(get_db)):
    email = normalize_email(payload.email)
    user = db.query(UserModel).filter(UserModel.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверная почта или пароль.")

    create_session(db, user, response)
    return {"user": public_user(user)}


@app.get("/api/auth/me")
def me(user: UserModel = Depends(get_current_user)):
    return {"user": public_user(user)}


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_token = request.cookies.get(COOKIE_NAME)
    if raw_token:
        db.query(AuthSessionModel).filter(
            AuthSessionModel.token_hash == token_digest(raw_token)
        ).delete(synchronize_session=False)
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax", secure=COOKIE_SECURE)


@app.post("/api/auth/forgot-password")
def forgot_password(payload: ForgotPasswordSchema, db: Session = Depends(get_db)):
    email = normalize_email(payload.email)
    user = db.query(UserModel).filter(UserModel.email == email).first()
    generic_message = "Если аккаунт существует, инструкция отправлена на указанную почту."
    if not user:
        return {"message": generic_message}

    now = int(time.time())
    raw_token = secrets.token_urlsafe(32)
    db.query(PasswordResetModel).filter(
        PasswordResetModel.user_id == user.id,
        PasswordResetModel.used_at.is_(None),
    ).delete(synchronize_session=False)
    reset_record = PasswordResetModel(
        token_hash=token_digest(raw_token),
        user_id=user.id,
        expires_at=now + RESET_TTL_SECONDS,
    )
    db.add(reset_record)
    db.commit()

    base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    reset_url = f"{base_url}/#reset-token={raw_token}"
    try:
        delivered = send_reset_email(email, reset_url)
    except Exception:
        db.delete(reset_record)
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Не удалось отправить письмо. Проверьте настройки почтового сервера.",
        )

    result = {"message": generic_message}
    if not delivered and ENVIRONMENT != "production":
        result.update(
            {
                "development_reset_token": raw_token,
                "message": "SMTP не настроен. В режиме разработки можно продолжить сброс прямо здесь.",
            }
        )
    return result


@app.post("/api/auth/reset-password")
def reset_password(payload: ResetPasswordSchema, db: Session = Depends(get_db)):
    validate_password(payload.password)
    now = int(time.time())
    reset_record = (
        db.query(PasswordResetModel)
        .filter(
            PasswordResetModel.token_hash == token_digest(payload.token),
            PasswordResetModel.used_at.is_(None),
            PasswordResetModel.expires_at > now,
        )
        .first()
    )
    if not reset_record:
        raise HTTPException(status_code=400, detail="Ссылка недействительна или уже истекла.")

    user = db.query(UserModel).filter(UserModel.id == reset_record.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Ссылка недействительна.")

    user.password_hash = hash_password(payload.password)
    reset_record.used_at = now
    db.query(AuthSessionModel).filter(AuthSessionModel.user_id == user.id).delete(
        synchronize_session=False
    )
    db.commit()
    return {"message": "Пароль обновлён. Теперь можно войти с новым паролем."}


@app.get("/api/curriculum")
def get_curriculum(
    user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)
):
    curriculum = (
        db.query(CurriculumModel).filter(CurriculumModel.owner_id == user.id).first()
    )
    return {"curriculum": public_curriculum(curriculum) if curriculum else None}


@app.put("/api/curriculum")
def save_curriculum(
    payload: CurriculumSchema,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    curriculum_name, categories, progress = normalize_curriculum(payload)
    now = int(time.time())
    curriculum = (
        db.query(CurriculumModel).filter(CurriculumModel.owner_id == user.id).first()
    )
    if curriculum:
        curriculum.name = curriculum_name
        curriculum.categories = categories
        curriculum.progress = progress
        curriculum.updated_at = now
    else:
        curriculum = CurriculumModel(
            owner_id=user.id,
            name=curriculum_name,
            categories=categories,
            progress=progress,
            updated_at=now,
        )
        db.add(curriculum)
    db.commit()
    db.refresh(curriculum)
    return {"curriculum": public_curriculum(curriculum)}


@app.get("/api/courses", response_model=List[CourseSchema])
def get_courses(
    user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)
):
    items = db.query(SavedCourseModel).filter(SavedCourseModel.owner_id == user.id).all()
    return [
        CourseSchema(
            id=item.course_uid,
            code=item.course_code,
            name=item.name,
            color=item.color,
            lectures=item.lectures,
            practices=item.practices,
        )
        for item in items
    ]


@app.post("/api/courses", response_model=List[CourseSchema])
def save_courses(
    courses: List[CourseSchema],
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(SavedCourseModel).filter(SavedCourseModel.owner_id == user.id).delete(
        synchronize_session=False
    )
    for course in courses:
        db.add(
            SavedCourseModel(
                owner_id=user.id,
                course_uid=course.id,
                course_code=(course.code or "").strip().upper()[:40] or None,
                name=course.name,
                color=course.color,
                lectures=course.lectures,
                practices=course.practices,
            )
        )
    db.commit()
    return courses


@app.get("/api/transcript", response_model=List[TranscriptSchema])
def get_transcript(
    _user: UserModel = Depends(get_current_user), db: Session = Depends(get_db)
):
    items = db.query(TranscriptModel).all()
    if not items:
        default_items = [
            TranscriptModel(code="MATH101", name="Calculus I", credits=5, status="completed"),
            TranscriptModel(code="ENG101", name="Academic Writing", credits=4, status="completed"),
            TranscriptModel(
                code="CS101", name="Intro to Programming", credits=5, status="in-progress"
            ),
        ]
        db.add_all(default_items)
        db.commit()
        items = db.query(TranscriptModel).all()
    return items


if os.path.exists(PUBLIC_DIR):
    app.mount("/static", StaticFiles(directory=PUBLIC_DIR), name="static")


@app.get("/")
def read_index():
    html_path = os.path.join(PUBLIC_DIR, "schedule-builder.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail=f"HTML file not found: {html_path}")
    return FileResponse(html_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
