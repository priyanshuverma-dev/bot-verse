from flask import Flask, Blueprint, request, jsonify, session, Response, send_file
from fpdf import FPDF
import re
import os
import uuid
from sqlalchemy import func
from .models import User, Chatbot, Chat, Image, Comment, ChatbotVersion
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from flask_login import login_user
from typing import Union, List, Optional, Dict
from .ai import chat_with_chatbot, text_to_mp3, translate_text, generate_image_caption
from .constants import BOT_AVATAR_API, USER_AVATAR_API
from .helpers import create_default_chatbots
from .data_fetcher import fetch_contribution_data
from datetime import datetime
import PIL
import pytesseract
import re
from flask_jwt_extended import (
    create_access_token,
    jwt_required,
    get_jwt_identity,
    current_user,
    verify_jwt_in_request,
)


ANONYMOUS_MESSAGE_LIMIT = 5

api_bp = Blueprint("api", __name__)
db = None
bcrypt = None


def register_api_routes(app: Flask, database, bcrypt_instance) -> None:
    global db, bcrypt
    db = database
    bcrypt = bcrypt_instance
    app.register_blueprint(api_bp)


def is_strong_password(password: str) -> bool:
    """Check if the password meets strength criteria."""
    if (
        len(password) < 8
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"[0-9]", password)
        or not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password)
    ):
        return False
    return True


def get_current_user():
    uid: str = get_jwt_identity()
    user = User.query.get(uid)
    return user


@api_bp.route("/api/login", methods=["POST"])
def api_login() -> Union[Response, tuple[Response, int]]:
    """API endpoint to log in a user."""
    login_type: str = request.args.get("type", "jwt").lower()
    if login_type == "session":
        username: str = request.form["username"]
        password: str = request.form["password"]
    else:
        data = request.get_json()

        username: str = data.get("username")
        password: str = data.get("password")

    user: Optional[User] = User.query.filter_by(username=username).first()
    if user and bcrypt.check_password_hash(user.password, password):
        # temp. condition
        # TODO: keep only jwt
        if login_type == "session":
            login_user(user)
            return jsonify({"success": True, "message": "User logged in successfully."})
        else:
            access_token = create_access_token(identity=user.id, expires_delta=False)
            return jsonify({"success": True, "access_token": access_token}), 200
    return (
        jsonify({"success": False, "message": "Invalid username or password."}),
        400,
    )


@api_bp.route("/api/signup", methods=["POST"])
def api_signup() -> Union[Response, tuple[Response, int]]:
    """API endpoint to sign up a new user."""
    login_type: str = request.args.get("type", "jwt").lower()

    if login_type == "session":
        username: str = request.form["username"]
        name: str = request.form["name"]
        password: str = request.form["password"]
        email: str = request.form["email"]
    else:
        data = request.get_json()

        username: str = data.get("username")
        name: str = data.get("name")
        password: str = data.get("password")
        email: str = data.get("email")
    if not is_strong_password(password):
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Password must be at least 8 characters long, include uppercase and lowercase letters, a number, and a special character.",
                }
            ),
            400,
        )
    existing_user = User.query.filter(
        (User.username == username) | (User.email == email)
    ).first()
    if existing_user:
        return (
            jsonify({"success": False, "message": "Username or email already exists."}),
            400,
        )

    hashed_password: str = bcrypt.generate_password_hash(password).decode("utf-8")
    avatar = f"{USER_AVATAR_API}/{name}"
    new_user: User = User(
        name=name,
        username=username,
        email=email,
        password=hashed_password,
        avatar=avatar,
        bio="I am Bot maker",
    )
    try:
        db.session.add(new_user)
        db.session.commit()
        return jsonify({"success": True, "message": "User registered successfully."})
    except IntegrityError:
        db.session.rollback()
        return (
            jsonify({"success": False, "message": "Username or email already exists."}),
            400,
        )


@api_bp.route("/api/create_chatbot", methods=["POST"])
@jwt_required()
def api_create_chatbot() -> Response:
    """API endpoint to create a new chatbot."""
    login_type: str = request.args.get("type", "jwt").lower()
    if login_type == "session":
        chatbot_name: str = request.form["chatbot_name"]
        chatbot_prompt: str = request.form["chatbot_prompt"]
    else:
        data = request.get_json()
        chatbot_name: str = data.get("name")
        chatbot_prompt: str = data.get("prompt")
        chatbot_category: str = data.get("category")

    user = get_current_user()
    chatbot: Chatbot = Chatbot(
        avatar=f"{BOT_AVATAR_API}/{chatbot_name}",
        user_id=user.id,
        public=False,
        category=chatbot_category,
        likes=0,
        reports=0,
    )
    db.session.add(chatbot)
    db.session.flush()

    chatbot.create_version(
        name=chatbot_name, new_prompt=chatbot_prompt, modified_by=user.username
    )

    user.contribution_score += 5
    db.session.commit()
    return jsonify({"success": True, "message": "Chatbot created."})


@api_bp.route("/api/chatbot/<int:chatbot_id>/update", methods=["POST"])
@jwt_required()
def api_update_chatbot(chatbot_id: int) -> Union[Response, str]:
    """API endpoint to update an existing chatbot."""
    chatbot: Chatbot = Chatbot.query.get_or_404(chatbot_id)
    user = get_current_user()
    if chatbot.user_id != user.id:
        return jsonify(
            {"success": False, "message": "You don't have permission for this action."}
        )

    data = request.get_json()
    new_name = data.get("name")
    new_prompt = data.get("prompt")
    new_category = data.get("category")
    chatbot.create_version(
        name=new_name, new_prompt=new_prompt, modified_by=user.username
    )

    chatbot.avatar = f"{BOT_AVATAR_API}/{new_name}"
    chatbot.category = new_category
    db.session.commit()
    return jsonify({"success": True, "message": "Chatbot Updated."})


@api_bp.route("/api/chatbot/<int:chatbot_id>/revert/<int:version_id>", methods=["POST"])
@jwt_required()
def api_revert_chatbot(chatbot_id: int, version_id: int) -> Union[Response, str]:
    """API endpoint to revert a chatbot to a previous version."""
    current_user = get_jwt_identity()

    # Fetch the chatbot by ID and ensure it belongs to the current user
    chatbot = Chatbot.query.filter_by(id=chatbot_id, user_id=current_user).first()
    if chatbot is None:
        return jsonify({"success": False, "message": "Chatbot not found."}), 404

    version = ChatbotVersion.query.filter_by(
        id=version_id, chatbot_id=chatbot_id
    ).first()
    if version is None:
        return jsonify({"success": False, "message": "Version not found."}), 404

    chatbot.latest_version_id = version.id

    try:
        db.session.commit()
        return jsonify(
            {
                "success": True,
                "message": "Chatbot reverted to the selected version.",
                "version": version.to_dict(),
            }
        )
    except Exception as e:
        db.session.rollback()
        return (
            jsonify(
                {
                    "success": False,
                    "message": "Failed to revert the chatbot.",
                    "error": str(e),
                }
            ),
            500,
        )


@api_bp.route("/api/delete/<string:obj>/<int:obj_id>", methods=["POST"])
@jwt_required()
def api_delete_chatbot(obj: str, obj_id: int) -> Union[Response, tuple[Response, int]]:
    """API endpoint to delete a chatbot."""
    valid_objs = {
        "chatbot": Chatbot,
        "image": Image,
    }
    if obj not in valid_objs:
        return jsonify({"success": False, "message": "Invalid obj"}), 400

    model = valid_objs[obj]
    item = db.session.query(model).filter_by(id=obj_id).first()
    user = get_current_user()
    if item.user_id != user.id:
        return (
            jsonify({"error": "Unauthorized access."}),
            403,
        )
    if obj == "chatbot":
        ChatbotVersion.query.filter_by(chatbot_id=obj_id).delete()
    db.session.delete(item)
    db.session.commit()

    return (
        jsonify({"message": f"{item} has been deleted successfully."}),
        200,
    )


@api_bp.route("/api/chatbot/<int:chatbot_id>", methods=["POST", "GET"])
@jwt_required()
def api_chatbot(chatbot_id: int) -> Union[Response, tuple[Response, int]]:
    """API endpoint to interact with a chatbot."""

    chatbot: Chatbot = Chatbot.query.get_or_404(chatbot_id)
    user = get_current_user()

    if (
        chatbot.user_id != user.id
        and not chatbot.public
        and chatbot.generated_by != "system"
    ):
        return jsonify({"success": False, "message": "Access denied."}), 403

    chats: List[Chat] = Chat.query.filter_by(
        chatbot_id=chatbot_id, user_id=user.id
    ).all()

    if request.method == "GET":
        return (
            jsonify(
                {
                    "success": True,
                    "bot": chatbot.to_dict(),
                    "chats": [chat.to_dict() for chat in chats],
                }
            ),
            200,
        )

    data = request.get_json()
    query: str = data.get("query")
    apikey = request.headers["apikey"]
    engine = request.headers["engine"]
    chat_to_pass: List[Dict[str, str]] = [
        {"role": "system", "content": chatbot.latest_version.prompt}
    ]
    for chat in chats:
        chat_to_pass.append({"role": "user", "content": chat.user_query})
        chat_to_pass.append({"role": "assistant", "content": chat.response})
    chat_to_pass.append({"role": "user", "content": query})

    response: Optional[str] = chat_with_chatbot(chat_to_pass, apikey, engine)

    if response:
        chat = Chat(
            chatbot_id=chatbot_id,
            user_id=user.id,
            user_query=query,
            response=response,
        )
        db.session.add(chat)
        db.session.commit()

        return jsonify({"success": True, "response": response})

    return (
        jsonify(
            {
                "success": False,
                "message": "Failed to get a response from the chatbot.",
            }
        ),
        500,
    )


@api_bp.route("/api/publish/<string:obj>/<int:obj_id>", methods=["POST"])
@jwt_required()
def api_publish_chatbot(obj: str, obj_id: int) -> Union[Response, tuple[Response, int]]:
    """API endpoint to publish or unpublish a chatbot."""
    valid_objs = {
        "chatbot": Chatbot,
        "image": Image,
    }
    if obj not in valid_objs:
        return jsonify({"success": False, "message": "Invalid obj"}), 400

    model = valid_objs[obj]
    item = db.session.query(model).filter_by(id=obj_id).first()

    if not item:
        return (
            jsonify({"success": False, "message": f"{obj.capitalize()} not found"}),
            404,
        )
    user = get_current_user()
    if item.user_id != user.id:
        return (
            jsonify({"error": "Unauthorized access."}),
            403,
        )

    item.public = not item.public
    user.contribution_score += 2
    db.session.commit()

    message: str = f"{item} is now {'published' if item.public else 'unpublished'}."

    return jsonify({"message": message, "public": item.public}), 200


@api_bp.route("/api/profile/edit", methods=["POST"])
@jwt_required()
def api_profile_edit() -> Union[Response, tuple[Response, int]]:
    """API endpoint to edit the user's profile."""
    user = get_current_user()
    data = request.get_json()
    username: str = data.get("username")
    name: str = data.get("name")
    bio: str = data.get("bio")

    user.name = name
    user.username = username
    user.bio = bio
    try:
        db.session.commit()
        return (
            jsonify({"message": "Profile updated successfully.", "success": True}),
            200,
        )
    except IntegrityError:
        db.session.rollback()
        return (
            jsonify({"message": "Username already exists.", "success": False}),
            400,
        )


@api_bp.route("/api/anonymous", methods=["POST"])
def api_anonymous_chatbot() -> Union[Response, tuple[Response, int]]:
    """API endpoint to interact with a chatbot."""
    try:
        verify_jwt_in_request(optional=True)
        is_authenticated = current_user.is_authenticated
    except Exception:
        is_authenticated = False
    if not is_authenticated:
        if "anonymous_message_count" not in session:
            session["anonymous_message_count"] = 0
            session["first_message_time"] = (
                datetime.now().isoformat()
            )

        session["anonymous_message_count"] += 1

        if session["anonymous_message_count"] > ANONYMOUS_MESSAGE_LIMIT:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Anonymous users are limited to 5 messages.",
                    }
                ),
                429,
            )

    data = request.get_json()
    prev_chats = data.get("prev")
    query: str = data.get("query")
    apikey = request.headers["apikey"]
    chat_to_pass: List[Dict[str, str]] = []
    engine = request.headers["engine"]
    for chat in prev_chats:
        chat_to_pass.append({"role": "user", "content": chat["user_query"]})
        chat_to_pass.append({"role": "assistant", "content": chat["response"]})
    chat_to_pass.append({"role": "user", "content": query})

    response: Optional[str] = chat_with_chatbot(chat_to_pass, apikey, engine)

    return jsonify(
        {
            "success": True,
            "response": response,
            "updated_chats": chat_to_pass,
        }
    )


@api_bp.route("/api/chatbot/<int:chatbot_id>/clear", methods=["POST"])
@jwt_required()
def api_clear_chats(chatbot_id: int) -> Union[Response, tuple[Response, int]]:
    """API endpoint to clear messages of a chatbot."""
    user = get_current_user()
    deleted_count = Chat.query.filter_by(
        chatbot_id=chatbot_id,
        user_id=user.id,
    ).delete()
    db.session.commit()

    return (
        jsonify(
            {
                "success": True,
                "message": f"Deleted {deleted_count} messages for chatbot ID {chatbot_id}.",
            }
        ),
        200,
    )


@api_bp.route("/api/imagine", methods=["POST", "GET"])
@jwt_required()
def api_create_image() -> Response:
    """API endpoint to create a new image."""

    user = get_current_user()
    if request.method == "GET":
        images: List[Image] = Image.query.filter_by(user_id=user.id).all()
        return jsonify(
            {"success": True, "images": [image.to_dict() for image in images]}
        )
    else:
        data = request.get_json()
        prompt: str = data.get("query")
        image: Image = Image(
            prompt=prompt,
            user_id=user.id,
        )

        db.session.add(image)
        user.contribution_score += 5
        db.session.commit()
        return jsonify({"success": True, "message": "Image created."})


@api_bp.route("/api/user_info", methods=["GET"])
@jwt_required()
def api_user_info():
    try:
        uid: int = get_jwt_identity()
        user = User.query.get(uid)
        if user is None:
            return jsonify({"success": False, "message": "User not found."}), 404

        return jsonify({"success": True, "user": user.to_dict()}), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/user/<string:username>", methods=["GET"])
@jwt_required()
def api_get_user_data(username: str):
    try:
        user: Optional[User] = User.query.filter_by(username=username).first()
        if user == None:
            return jsonify({"success": False, "message": "User not found"}), 404

        num_chatbots = Chatbot.query.filter(Chatbot.user_id == user.id).count()
        num_images = Image.query.filter(Image.user_id == user.id).count()
        POINTS_PER_BOT = 5
        POINTS_PER_IMAGE = 1
        contribution_score = (num_chatbots * POINTS_PER_BOT) + (
            num_images * POINTS_PER_IMAGE
        )

        response = {
            "success": True,
            "user": user.to_dict(),
            "contribution_score": contribution_score,
        }
        return jsonify(response), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/data", methods=["GET"])
@jwt_required()
def api_get_data():
    try:
        create_default_chatbots(db)
        uid: str = get_jwt_identity()
        queues_req: str = request.args.get("queues")
        o_uid: str = request.args.get("uid")
        if queues_req:
            queues: List[str] = queues_req.split(",")
        else:
            queues = []

        valid_queues = {
            "system_bots",
            "my_bots",
            "my_images",
            "public_bots",
            "public_images",
            "user_bots",
            "trend_today",
            "leaderboard",
            "user_images",
        }
        queues = [q for q in queues if q in valid_queues]
        response = {"success": True}

        if "system_bots" in queues:
            system_chatbots: List[Chatbot] = (
                Chatbot.query.options(
                    joinedload(Chatbot.latest_version)
                )
                .filter(
                    Chatbot.latest_version.has(modified_by="system")
                )
                .all()
            )
            response["system_bots"] = [bot.to_dict() for bot in system_chatbots]
        if "my_bots" in queues:
            chatbots: List[Chatbot] = Chatbot.query.filter(Chatbot.user_id == uid).all()
            response["my_bots"] = [bot.to_dict() for bot in chatbots]
        if "my_images" in queues:
            images: List[Image] = Image.query.filter(Image.user_id == uid).all()
            response["my_images"] = [image.to_dict() for image in images]
        if "public_bots" in queues:
            public_chatbots: List[Chatbot] = Chatbot.query.filter_by(public=True).all()
            response["public_bots"] = [bot.to_dict() for bot in public_chatbots]
        if "public_images" in queues:
            public_images: List[Image] = Image.query.filter_by(public=True).all()
            response["public_images"] = [image.to_dict() for image in public_images]
        if "user_bots" in queues:
            o_chatbots: List[Chatbot] = Chatbot.query.filter(
                Chatbot.user_id == o_uid
            ).all()
            response["user_bots"] = [bot.to_dict() for bot in o_chatbots]
        if "user_images" in queues:
            o_images: List[Image] = Image.query.filter(Image.user_id == o_uid).all()
            response["user_images"] = [image.to_dict() for image in o_images]

        if "trend_today" in queues:
            chatbot_of_the_day: Chatbot = (
                db.session.query(Chatbot)
                .filter(Chatbot.public == True)
                .order_by(func.random())
                .first()
            )
            image_of_the_day: Image = (
                db.session.query(Image)
                .filter(Image.public == True)
                .order_by(func.random())
                .first()
            )
            response["trend_today"] = {
                "chatbot": (
                    chatbot_of_the_day.to_dict() if chatbot_of_the_day else None
                ),
                "image": (image_of_the_day.to_dict() if image_of_the_day else None),
            }

        if "leaderboard" in queues:
            users = fetch_contribution_data(db)
            response["leaderboard"] = [user.to_dict() for user in users]

        return jsonify(response), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/actions/<string:obj>/<int:obj_id>/like", methods=["POST"])
def api_like(obj, obj_id):
    try:
        valid_objs = {
            "chatbot": Chatbot,
            "image": Image,
            "user": User,
            "comment": Comment,
        }
        if obj not in valid_objs:
            return jsonify({"success": False, "message": "Invalid obj"}), 400

        model = valid_objs[obj]
        item = db.session.query(model).filter_by(id=obj_id).first()

        if not item:
            return (
                jsonify({"success": False, "message": f"{obj.capitalize()} not found"}),
                404,
            )

        item.likes += 1

        db.session.commit()
        return (
            jsonify(
                {"success": True, "message": f"{obj.capitalize()} liked successfully!"}
            ),
            200,
        )

    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/actions/<string:obj>/<int:obj_id>/report", methods=["POST"])
def api_report(obj, obj_id):
    try:
        valid_objs = {
            "chatbot": Chatbot,
            "image": Image,
            "user": User,
            "comment": Comment,
        }
        if obj not in valid_objs:
            return jsonify({"success": False, "message": "Invalid obj"}), 400

        model = valid_objs[obj]
        item = db.session.query(model).filter_by(id=obj_id).first()

        if not item:
            return (
                jsonify({"success": False, "message": f"{obj.capitalize()} not found"}),
                404,
            )

        item.reports += 1

        db.session.commit()
        return (
            jsonify(
                {"success": True, "message": f"{obj.capitalize()} liked successfully!"}
            ),
            200,
        )
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/chatbot_data/<int:chatbot_id>", methods=["GET"])
@jwt_required()
def api_get_chatbot_data(chatbot_id: str):
    try:
        chatbot: Chatbot = Chatbot.query.get(chatbot_id)
        if chatbot == None:
            return jsonify({"success": False, "message": "Chatbot not found"}), 404
        versions = (
            ChatbotVersion.query.filter_by(chatbot_id=chatbot_id)
            .order_by(ChatbotVersion.version_number.desc())
            .all()
        )
        comments: List[Comment] = Comment.query.filter_by(chatbot_id=chatbot_id).all()
        return (
            jsonify(
                {
                    "success": True,
                    "bot": chatbot.to_dict(),
                    "versions": [version.to_dict() for version in versions],
                    "comments": [comment.to_dict() for comment in comments],
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/chatbot/comment", methods=["POST"])
@jwt_required()
def api_comment_chatbot():
    try:
        data = request.get_json()
        chatbot_id = data.get("chatbotId")
        name = data.get("name")
        message = data.get("message")
        chatbot: Chatbot = Chatbot.query.get(chatbot_id)
        if chatbot == None:
            return jsonify({"success": False, "message": "Chatbot not found"}), 404
        comment: Comment = Comment(
            name=name,
            chatbot_id=chatbot_id,
            message=message,
        )
        db.session.add(comment)
        user = get_current_user()
        if user:
            user.contribution_score += 3
        db.session.commit()
        return jsonify({"success": True, "message": "Comment saved"}), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/tts", methods=["POST"])
@jwt_required()
def api_tts():
    try:
        data = request.get_json()
        text = data.get("text")
        if not text:
            return jsonify({"success": False, "message": "Text not found"}), 400

        filepath = text_to_mp3(text)

        response = send_file(filepath, as_attachment=True)

        @response.call_on_close
        def remove_file():
            os.remove(filepath)

        return response

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/translate", methods=["POST"])
@jwt_required()
def api_translate():
    try:
        data = request.get_json()
        text = data.get("text")
        to_language = data.get("to_language")
        from_lang = data.get("from_language")
        if not text and not to_language:
            return (
                jsonify({"success": False, "message": "Text or language not found"}),
                400,
            )

        translated = translate_text(text, from_lang=from_lang, target_lang=to_language)

        return jsonify({"success": True, "translated": translated}), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/ocr", methods=["POST"])
@jwt_required()
def api_ocr():
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "No file provided"}), 400

        file = request.files["file"]
        base_path = os.path.dirname(os.path.abspath(__file__))
        temp_audio_dir = os.path.join(base_path, "temp_images")
        os.makedirs(temp_audio_dir, exist_ok=True)
        filepath = os.path.join(temp_audio_dir, file.filename)
        file.save(filepath)

        image = PIL.Image.open(filepath)
        text = pytesseract.image_to_string(image)

        os.remove(filepath)
        return jsonify({"success": True, "text": text}), 200

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


class HandwrittenPDF(FPDF):
    def header(self):
        pass

    def footer(self):
        pass

    def add_custom_font(self, font_name, font_path):
        self.add_font(font_name, "", font_path, uni=True)


@api_bp.route("/api/tth", methods=["POST"])
@jwt_required()
def api_tth():
    try:
        data = request.get_json()
        text = data.get("text", "")
        font_size = data.get("font_size", 12)
        pdf = HandwrittenPDF()

        custom_font_name = "Handwritten"
        font_path = os.path.join(os.path.dirname(__file__), "fonts", "handwriting.ttf")
        pdf.add_custom_font(custom_font_name, font_path)

        pdf.add_page()
        pdf.set_font(custom_font_name, size=font_size)
        pdf.set_text_color(0, 0, 255)

        line_height = font_size * 0.9
        margin = 10
        page_width = pdf.w - 2 * margin
        pdf.set_left_margin(margin)
        pdf.set_right_margin(margin)

        lines = text.split("\n")
        for line in lines:
            words = line.split()
            current_line = ""
            for word in words:
                test_line = f"{current_line} {word}".strip()
                if pdf.get_string_width(test_line) <= page_width:
                    current_line = test_line
                else:
                    pdf.cell(0, line_height, current_line, ln=True)
                    current_line = word
            if current_line:
                pdf.cell(0, line_height, current_line, ln=True)
            pdf.ln(line_height * 0.2)

        base_path = os.path.dirname(
            os.path.abspath(__file__)
        )
        temp_dir = os.path.join(base_path, "temp_pdfs")
        os.makedirs(temp_dir, exist_ok=True)
        output_path = f"{temp_dir}/{uuid.uuid4()}.pdf"
        pdf.output(output_path)

        response = send_file(output_path, as_attachment=True)

        @response.call_on_close
        def remove_file():
            os.remove(output_path)

        return response
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@api_bp.route("/api/image-captioning", methods=["POST"])
@jwt_required()
def api_image_captioning():
    if "image" not in request.files:
        return jsonify({"success": False, "message": "No image file provided."}), 400

    file = request.files["image"]
    if file.filename == "":
        return jsonify({"success": False, "message": "No selected file"}), 400

    try:
        image_data = file.read()
        caption = generate_image_caption(image_data)
        return jsonify({"success": True, "caption": caption})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
