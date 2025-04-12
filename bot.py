import logging
import os
import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import gc
import os
import tempfile
import base64
import time
import cv2
from openai import OpenAI
from supabase import create_client, Client


# --- Load Environment Variables ---
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
SUPABASE_BUCKET_NAME = os.getenv("SUPABASE_BUCKET_NAME")
EMAIL, PASSWORD = range(2)
GET_TITLE, GET_DESCRIPTION, GET_DATE = range(3)

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

authenticated_users = {}
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends explanation on /start."""
    await update.message.reply_text(
        "Hi! Send /login to authenticate, then send me images."
    )
    
async def exit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs the user out by removing their ID from authenticated_users."""
    user_id = update.effective_user.id
    if user_id in authenticated_users:
        del authenticated_users[user_id]
        logger.info(f"User {user_id} logged out.")
        await update.message.reply_text("You have been logged out.")
    else:
        logger.info(f"User {user_id} attempted /exit but was not logged in.")
        await update.message.reply_text("You are not currently logged in.")
    
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the login conversation."""
    await update.message.reply_text("Please enter your email address:")
    return EMAIL # Next state is waiting for EMAIL

async def received_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores email and asks for password."""
    user_email = update.message.text
    context.user_data['email'] = user_email # Store email temporarily
    logger.info(f"Received email from {update.effective_user.id}: {user_email}")
    await update.message.reply_text("Thank you. Now, please enter your password:")
    return PASSWORD # Next state is waiting for PASSWORD

async def received_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives password, attempts Supabase auth."""
    user_password = update.message.text
    user_email = context.user_data.get('email')
    user_id = update.effective_user.id

    if not user_email:
        await update.message.reply_text("Something went wrong, please start /login again.")
        return ConversationHandler.END

    logger.info(f"Attempting login for email: {user_email}")
    try:
        # --- Use Supabase Auth sign_in_with_password ---
        # This is the RECOMMENDED way vs querying a table with plain passwords
        session_response = supabase.auth.sign_in_with_password({"email": user_email, "password": user_password})

        if session_response and session_response.user:
            supabase_user_id = session_response.user.id
            logger.info(f"Login successful for {user_email}, Supabase User ID: {supabase_user_id}")
            # Store the actual Supabase User ID associated with the Telegram user
            authenticated_users[user_id] = supabase_user_id # STORE THE REAL USER ID
            await update.message.reply_text("Login successful! You can now send me images.")
            context.user_data.clear() # Clear temporary login data
            return ConversationHandler.END
        else:
            # Handle cases where sign_in_with_password might return None or no user
            logger.warning(f"Login failed for {user_email}. Response: {session_response}")
            await update.message.reply_text("Login failed. Incorrect email or password. Try /login again.")
            context.user_data.clear()
            return ConversationHandler.END

    except Exception as e: # Catch specific auth errors if possible from Supabase lib
        logger.error(f"Error during Supabase sign_in for {user_email}: {e}")
        await update.message.reply_text("An error occurred during login. Please try again later.")
        context.user_data.clear()
        return ConversationHandler.END

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the login process."""
    await update.message.reply_text("Login cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log Errors caused by Updates."""
    logger.error(f"Update {update} caused error {context.error}")

async def handle_media_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the media post conversation. Handles image/video upload."""
    user_id = update.effective_user.id
    message = update.message

    if user_id not in authenticated_users:
        await message.reply_text("Please /login first before sending media.")
        return ConversationHandler.END

    supabase_user_id = authenticated_users.get(user_id)
    if not supabase_user_id:
         logger.error(f"Authenticated user {user_id} has no Supabase ID stored.")
         await message.reply_text("Authentication error. Please /login again.")
         return ConversationHandler.END

    file_id = None
    file_info = None
    media_type = None # 'photo' or 'video'
    file_extension = '.jpg' # Default for photo/thumbnail frame
    mime_type = 'image/jpeg' # Default

    if message.photo:
        media_type = 'photo'
        file_info = message.photo[-1]
        file_id = file_info.file_id
        mime_type = 'image/jpeg'
        logger.info(f"Received photo from user {user_id}")
    elif message.video:
        media_type = 'video'
        file_info = message.video
        file_id = file_info.file_id
        mime_type = file_info.mime_type or 'video/mp4'
        if file_info.file_name:
            _, ext = os.path.splitext(file_info.file_name)
            if ext:
                file_extension = ext.lower()
        else:
            if 'mp4' in mime_type: file_extension = '.mp4'
            elif 'quicktime' in mime_type: file_extension = '.mov'
            elif 'webm' in mime_type: file_extension = '.webm'
            else: file_extension = '.mp4'
        logger.info(f"Received video from user {user_id} (MIME: {mime_type}, Ext: {file_extension})")
    else:
        # Should not happen if filter is PHOTO | VIDEO, but good practice
        logger.warning(f"Received non-photo/video message in media handler from user {user_id}")
        await message.reply_text("Please send a photo or video.")
        return None
    
    temp_file_path = None
    processing_msg = await message.reply_text(f"Processing {media_type}... ⏳")

    try:
        # --- Download Media ---
        logger.info(f"Downloading {media_type} with file_id: {file_id}")
        downloaded_file = await context.bot.get_file(file_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
            await downloaded_file.download_to_drive(tmp_file.name)
            temp_file_path = tmp_file.name
        logger.info(f"Media downloaded to temporary file: {temp_file_path}")

        # --- Prepare Filename for Storage ---
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')
        # Use a base name, extension will be added by upload function logic if needed or use determined one
        storage_filename = f"{timestamp}_{file_id}{file_extension}"

        # --- Upload Original Media to Supabase ---
        image_upload_success = await upload_to_supabase_storage(
            temp_file_path, # Pass the file path now
            storage_filename,
            supabase_user_id,
            mime_type
        )

        if not image_upload_success:
            await processing_msg.edit_text(f"Failed to upload {media_type} to storage. Cannot proceed.")
            return ConversationHandler.END

        # --- Construct Public URL ---
        storage_path = f"memories/{supabase_user_id}/{storage_filename}"
        media_public_url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{SUPABASE_BUCKET_NAME}/{storage_path}"
        logger.info(f"{media_type.capitalize()} uploaded. Public URL: {media_public_url}")

        context.user_data['storage_path'] = storage_path    # Save relative path for DB insertion
        context.user_data['media_public_url'] = media_public_url
        context.user_data['supabase_user_id'] = supabase_user_id

        # --- Prepare Data for AI (Extract Frame if Video) ---
        bytes_for_ai = None
        if media_type == 'photo':
            with open(temp_file_path, 'rb') as f:
                bytes_for_ai = f.read()
        elif media_type == 'video':
            logger.info("Extracting frame from video for AI analysis...")
            cap = None # Initialize cap outside try
            try:
                cap = cv2.VideoCapture(temp_file_path)
                if cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        is_success, buffer = cv2.imencode(".jpg", frame)
                        if is_success:
                            bytes_for_ai = buffer.tobytes()
                            logger.info("Successfully extracted and encoded video frame.")
                        else:
                            logger.warning("Could not encode extracted video frame.")
                    else:
                        logger.warning("Could not read frame from video file.")
                else:
                    logger.warning(f"Could not open video file {temp_file_path} with OpenCV.")
            finally:
                # Ensure release happens even if errors occur during frame processing
                if cap is not None and cap.isOpened():
                    cap.release()
                    logger.info(f"OpenCV VideoCapture released for {temp_file_path}")
                gc.collect()
                
        time.sleep(0.5) # Wait half a second before trying to delete
        
        if bytes_for_ai:
            context.user_data['media_bytes_for_ai'] = bytes_for_ai
        else:
            context.user_data['media_bytes_for_ai'] = None
            logger.warning("No bytes available for AI processing.")

        context.user_data['media_public_url'] = media_public_url
        context.user_data['supabase_user_id'] = supabase_user_id
        
        await processing_msg.edit_text(f"{media_type.capitalize()} uploaded! Now, please enter a TITLE:")
        logger.info(f"Transitioning to GET_TITLE state for user {user_id}")
        return GET_TITLE # Transition to the state waiting for title

    except Exception as e:
        logger.error(f"Critical error handling {media_type} entry for {user_id}: {e}", exc_info=True)
        await processing_msg.edit_text(f"Sorry, a critical error occurred while processing your {media_type}.")
        return ConversationHandler.END
    
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            logger.info(f"Attempting cleanup of temporary file: {temp_file_path}")
            # Add a delay specifically for Windows file locking issues AFTER cap.release()
            time.sleep(0.5) # Wait half a second before trying to delete
            try:
                os.remove(temp_file_path)
                logger.info(f"Successfully removed temporary file: {temp_file_path}")
            except PermissionError as pe:
                 logger.error(f"PermissionError cleaning up temp file {temp_file_path}: {pe}. File might still be locked.")
            except FileNotFoundError:
                 logger.warning(f"Temporary file {temp_file_path} not found during cleanup (already deleted?).")
            except Exception as cleanup_e:
                 logger.error(f"Generic error cleaning up temp file {temp_file_path}: {cleanup_e}")
        
async def received_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id # Get user ID for logging
    logger.info(f"--- Entered received_title function for user {user_id} ---") # Add this
    title = update.message.text
    context.user_data['title'] = title
    logger.info(f"Stored title for user {user_id}: {title}")

    try:
        await update.message.reply_text("Got it. Now, please enter a short description for this memory:")
        logger.info(f"Asked user {user_id} for description.") # Add this
        logger.info(f"Transitioning to GET_DESCRIPTION state for user {user_id}")
        return GET_DESCRIPTION # Move to next state
    except Exception as e:
        logger.error(f"Error sending 'ask description' message for user {user_id}: {e}", exc_info=True)
        # Decide how to handle - maybe end conversation?
        await update.message.reply_text("Sorry, an error occurred. Please try sending the image again.")
        context.user_data.clear()
        return ConversationHandler.END

async def received_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores description, triggers AI keywords then asks for event date."""
    description = update.message.text
    context.user_data['description'] = description  # Store description
    logger.info(f"Received description: {description}")
    
    # (Assuming you still want to trigger AI processing for keywords)
    processing_msg = await update.message.reply_text("Analyzing media for keywords... 🤖")
    
    media_bytes_for_ai = context.user_data.get('media_bytes_for_ai')
    keywords = []
    if (media_bytes_for_ai):
        keywords = await get_image_keywords_openai(media_bytes_for_ai)
        if not keywords:
            logger.warning("AI keyword generation failed or returned empty.")
            await update.message.reply_text("(Could not generate keywords, but will save the post anyway.)", quote=False)
        else:
            logger.info(f"Generated keywords: {keywords}")
    else:
        logger.warning("Skipping AI keyword generation as no frame/image data was available.")
        await update.message.reply_text("(Skipping keyword generation for this media.)", quote=False)
    
    # Store the keywords for later insertion (if needed)
    context.user_data['keywords'] = keywords

    # Instead of finalizing the post immediately, ask for the event date now.
    await processing_msg.edit_text("Almost done! When did this media happen? Please enter the date in yyyy/mm/dd format:")
    logger.info("Transitioning to GET_DATE state.")
    return GET_DATE  # Move to the new state for receiving date
    
    
async def received_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores date input from the user and finalizes post creation."""
    date_input = update.message.text.strip()
    user_id = update.effective_user.id
    try:
        # Validate date format (yyyy/mm/dd)
        event_date = datetime.datetime.strptime(date_input, "%Y/%m/%d").date()
        context.user_data['event_date'] = event_date.isoformat()  # e.g. "2025-04-11"
        logger.info(f"Stored event date for user {user_id}: {context.user_data['event_date']}")
    except ValueError as e:
        logger.error(f"Date parsing error for user {user_id}: {e}")
        await update.message.reply_text("Invalid date format. Please enter the date in yyyy/mm/dd format:")
        return GET_DATE  # Ask again if the format was invalid

    processing_msg = await update.message.reply_text("Saving your memory post...")
    
    # Retrieve all stored data
    storage_path = context.user_data.get('storage_path')
    title = context.user_data.get('title')
    description = context.user_data.get('description')
    supabase_user_id = context.user_data.get('supabase_user_id')
    keywords = context.user_data.get('keywords', [])
    
    if not all([storage_path, title, description, supabase_user_id]):
        logger.error("Missing essential data in conversation context. Cannot proceed.")
        await processing_msg.edit_text("Sorry, something went wrong. Please try again.")
        context.user_data.clear()
        return ConversationHandler.END

    # --- Insert into Database ---
    insert_success = await insert_post_to_supabase(
        supabase_user_id=supabase_user_id,
        image_storage_path=storage_path,
        title=title,
        description=description,
        keywords=keywords  # Pass potentially empty list
    )
    
    # --- Final Confirmation ---
    if insert_success:
        final_reply = f"Memory post saved successfully!\nTitle: {title}\nDate: {context.user_data['event_date']}"
        if keywords:
            final_reply += f"\nKeywords: {', '.join(keywords)}"
        await processing_msg.edit_text(final_reply)
    else:
        await processing_msg.edit_text("Sorry, there was an error saving your post to the database.")
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_post_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the post creation process."""
    await update.message.reply_text("Post creation cancelled.")
    # Clean up any potentially uploaded image if desired (more complex)
    context.user_data.clear()
    return ConversationHandler.END


async def upload_to_supabase_storage(file_path: str, filename: str, target_id: str, content_type: str) -> bool:
    """Uploads a file from a path to Supabase Storage."""
    if not all([SUPABASE_BUCKET_NAME, supabase]):
        logger.error("Supabase credentials, client, or bucket name not configured.")
        return False
    if not os.path.exists(file_path):
        logger.error(f"File path does not exist for upload: {file_path}")
        return False

    storage_path = f"memories/{target_id}/{filename}"
    logger.info(f"Attempting to upload '{file_path}' to Supabase Storage: Bucket={SUPABASE_BUCKET_NAME}, Path={storage_path}, ContentType={content_type}")

    try:
        # Use the file path directly with the upload function
        response = supabase.storage.from_(SUPABASE_BUCKET_NAME).upload(
            path=storage_path,
            file=file_path, # Pass the file path
            file_options={"content-type": content_type} # Use provided content type
        )
        logger.info(f"Successfully uploaded {storage_path} to Supabase Storage.")
        return True
    except Exception as e:
        logger.error(f"Failed to upload {storage_path} to Supabase Storage: {e}")
        return False
    # No need to manually clean up temp file here, it's done in the calling function
    
async def get_image_keywords_openai(image_bytes: bytes) -> list[str]:
    """Gets relevant keywords in Indonesian from OpenAI Vision API."""
    if not openai_client:
        logger.error("OpenAI client not configured.")
        return []
    if not image_bytes:
        logger.error("No image bytes received for AI keyword generation.")
        return []

    logger.info("Sending image to OpenAI Vision for Indonesian keywords...")
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        # Prompt specifically for Indonesian keywords
                        {"type": "text", "text": "Berikan 3-5 kata kunci (keywords) dalam Bahasa Indonesia yang relevan untuk gambar atau frame video ini, dipisahkan dengan koma."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "low"
                            }
                        },
                    ],
                }
            ],
            max_tokens=50,
        )
        content = response.choices[0].message.content.strip()
        logger.info(f"OpenAI keyword response received.")
        keywords = [k.strip() for k in content.split(',') if k.strip()]

        keywords = keywords[:5]

        logger.info(f"Parsed Indonesian Keywords: {keywords}")
        return keywords

    except Exception as e:
        logger.error(f"Error calling OpenAI Vision for keywords: {e}")
        return [] # Return empty list on error


async def insert_post_to_supabase(
    supabase_user_id: str,
    image_storage_path: str,
    title: str,
    description: str,
    keywords: list[str]
) -> bool:
    """Inserts post data into the 'posts' table in Supabase."""
    if not supabase:
        logger.error("Supabase client not configured for DB insert.")
        return False
    keywords_string = ", ".join(keywords)

    post_data = {
        'user_id': supabase_user_id, # Ensure this matches the foreign key to your auth.users table
        'image': image_storage_path,   # Column for the image URL
        'title': title,
        'caption': description,
        'memory_word': keywords_string # Column for keywords
    }

    logger.info(f"Attempting to insert post into Supabase table 'posts': {post_data}")
    try:
        response = supabase.table('posts').insert(post_data).execute()
        logger.info("Successfully inserted post into Supabase.")
        return True
    except Exception as e:
        logger.error(f"Failed to insert post into Supabase: {e}")
        return False




# --- Main Bot Execution ---
if __name__ == '__main__':
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN not found in environment variables!")
    else:
        logger.info("Starting bot...")
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        # --- Define Handlers ---

        login_conv_handler = ConversationHandler(
            entry_points=[CommandHandler('login', login_start)],
            states={
                EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_email)],
                PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_password)],
            },
            fallbacks=[CommandHandler('cancel', cancel_login)],
        )

        post_conv_handler = ConversationHandler(
            # Entry point handles photos and videos in private chats to start the flow
            entry_points=[MessageHandler(filters.PHOTO | filters.VIDEO & filters.ChatType.PRIVATE, handle_media_entry)],
            states={
                # State for when waiting for the title text
                GET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_title)],
                # State for when waiting for the description text
                GET_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_description)],
                GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_date)],
            },
            fallbacks=[CommandHandler('cancel', cancel_post_creation)],
        )

        application.add_handler(login_conv_handler)
        application.add_handler(post_conv_handler) # Handles photo entry for the post conversation

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("exit", exit_command))
        application.add_error_handler(error_handler)

        # --- Start the Bot ---
        logger.info("Bot polling...")
        application.run_polling()