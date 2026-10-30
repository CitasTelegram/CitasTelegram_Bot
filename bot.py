import os
import time
import logging
import threading
import asyncio
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# Configuración de Logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Variables de entorno
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

try:
    MI_TELEGRAM_ID = int(os.environ.get("MI_TELEGRAM_ID")) if os.environ.get("MI_TELEGRAM_ID") else None
except (TypeError, ValueError):
    MI_TELEGRAM_ID = None

# Estados de texto para registro y edición
USER_STATES = {}

# --- 1. BASE DE DATOS SUPABASE ---
def init_db():
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL no configurada.")
        return False
    for _ in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            # Tabla de Perfiles de Citas
            cur.execute("""
                CREATE TABLE IF NOT EXISTS perfiles_citas (
                    chat_id BIGINT PRIMARY KEY,
                    nombre_telegram TEXT,
                    alias TEXT,
                    ciudad TEXT,
                    descripcion TEXT,
                    estado_civil TEXT DEFAULT 'Soltero/a',
                    busca_genero TEXT DEFAULT 'ambos', -- 'hombre', 'mujer', 'ambos'
                    genero_propio TEXT DEFAULT 'otro', -- 'hombre', 'mujer', 'otro'
                    fotos TEXT[] DEFAULT ARRAY[]::TEXT[], -- hasta 6 fotos (file_ids de Telegram)
                    visible BOOLEAN DEFAULT TRUE,
                    online BOOLEAN DEFAULT TRUE,
                    creado_en TIMESTAMP DEFAULT NOW()
                );
            """)
            # Tabla de Likes
            cur.execute("""
                CREATE TABLE IF NOT EXISTS likes_citas (
                    id SERIAL PRIMARY KEY,
                    de_chat_id BIGINT NOT NULL,
                    para_chat_id BIGINT NOT NULL,
                    creado_en TIMESTAMP DEFAULT NOW(),
                    UNIQUE(de_chat_id, para_chat_id)
                );
            """)
            # Tabla de Conexiones (Match mutuo)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conexiones_citas (
                    id SERIAL PRIMARY KEY,
                    usuario1 BIGINT NOT NULL,
                    usuario2 BIGINT NOT NULL,
                    creado_en TIMESTAMP DEFAULT NOW()
                );
            """)
            # Tabla de Mensajes Internos
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mensajes_citas (
                    id SERIAL PRIMARY KEY,
                    de_chat_id BIGINT NOT NULL,
                    para_chat_id BIGINT NOT NULL,
                    mensaje TEXT NOT NULL,
                    leido BOOLEAN DEFAULT FALSE,
                    creado_en TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()
            cur.close()
            conn.close()
            logger.info("✅ Tablas de CitasTelegram listas en Supabase.")
            return True
        except Exception as e:
            logger.error(f"Error conectando a Supabase: {e}")
            time.sleep(2)
    return False

def obtener_perfil(chat_id):
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT alias, ciudad, descripcion, estado_civil, busca_genero, genero_propio, fotos, visible, online FROM perfiles_citas WHERE chat_id = %s;", (chat_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row
    except Exception as e:
        logger.error(f"Error obteniendo perfil: {e}")
        return None

# --- 2. SERVIDOR WEB ANTI-SLEEP PARA RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("CitasTelegram_Bot Activo 💖💘".encode("utf-8"))
    def log_message(self, format, *args):
        return

def start_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def keep_alive_ping():
    url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/" if RENDER_EXTERNAL_URL else None
    while True:
        time.sleep(300)
        if url:
            try:
                import requests
                requests.get(url, timeout=10)
            except Exception:
                pass

# --- 3. MENÚS Y TECLADOS ---
def teclado_principal(registrado=False, es_admin=False):
    keyboard = []
    if not registrado:
        keyboard.append([InlineKeyboardButton("✨ Registrarse en Citas", callback_data="menu_registro")])
    else:
        keyboard.append([InlineKeyboardButton("🔥 Descubrir Perfiles", callback_data="menu_descubrir")])
        keyboard.append([InlineKeyboardButton("👤 Mi Perfil & Fotos", callback_data="menu_perfil")])
        keyboard.append([InlineKeyboardButton("📬 Buzón (Likes, Conexiones, Mensajes)", callback_data="menu_buzon")])
        keyboard.append([InlineKeyboardButton("⚙️ Opciones / Borrar Cuenta", callback_data="menu_opciones")])
    
    if es_admin:
        keyboard.append([InlineKeyboardButton("🛡️ Panel de Administrador", callback_data="menu_admin")])
        
    return InlineKeyboardMarkup(keyboard)

def teclado_admin():
    keyboard = [
        [InlineKeyboardButton("📊 Estadísticas de Usuarios", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Listar Todos los Perfiles", callback_data="admin_listar")],
        [InlineKeyboardButton("⬅️ Volver al Menú Principal", callback_data="menu_principal")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- 4. HANDLERS DE TELEGRAM ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = user.id
    es_admin = (MI_TELEGRAM_ID is not None and chat_id == MI_TELEGRAM_ID)
    
    init_db()
    perfil = obtener_perfil(chat_id)
    registrado = (perfil is not None)

    welcome_text = (
        f"💖 ¡Bienvenido/a a CitasTelegram_Bot, {user.first_name}!\n\n"
        "El punto de encuentro para conocer gente nueva, conectar y chatear.\n"
        "Usa el menú inferior para empezar:"
    )
    await update.message.reply_text(text=welcome_text, reply_markup=teclado_principal(registrado, es_admin))

async def procesar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    chat_id = user.id
    es_admin = (MI_TELEGRAM_ID is not None and chat_id == MI_TELEGRAM_ID)

    perfil = obtener_perfil(chat_id)
    registrado = (perfil is not None)

    if query.data == "menu_principal":
        USER_STATES.pop(chat_id, None)
        await query.edit_message_text(
            "💖 Menú Principal - CitasTelegram",
            reply_markup=teclado_principal(registrado, es_admin)
        )

    elif query.data == "menu_admin":
        if not es_admin:
            await query.answer("⛔ Acceso denegado.", show_alert=True)
            return
        await query.edit_message_text("🛡️ Panel de Administración:", reply_markup=teclado_admin())

    elif query.data == "admin_stats":
        if not es_admin:
            return
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM perfiles_citas WHERE visible = TRUE;")
            total_activos = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM conexiones_citas;")
            total_conexiones = cur.fetchone()[0]
            cur.close()
            conn.close()
            msg = f"📊 Estadísticas:\n• Perfiles Activos: {total_activos}\n• Conexiones Totales: {total_conexiones}"
            kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=teclado_admin())

    elif query.data == "admin_listar":
        if not es_admin:
            return
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT chat_id, alias, ciudad, visible FROM perfiles_citas LIMIT 15;")
            filas = cur.fetchall()
            cur.close()
            conn.close()
            msg = "👥 Perfiles registrados:\n" + ("\n".join([f"• ID:{f[0]} | Alias:{f[1]} | Ciudad:{f[2]} | Visible:{f[3]}" for f in filas]) if filas else "Vacío")
            kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=teclado_admin())

    elif query.data == "menu_registro":
        USER_STATES[chat_id] = "REG_ALIAS"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu_principal")]]
        await query.edit_message_text("✨ **Asistente de Registro**\n\nPor favor, escribe tu **Nombre o Alias** para mostrar en tu perfil:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "menu_perfil":
        if not perfil:
            await query.answer("Primero debes registrarte.", show_alert=True)
            return
        alias, ciudad, descripcion, estado_civil, busca_genero, genero_propio, fotos, visible, online = perfil
        num_fotos = len(fotos) if fotos else 0
        
        msg = (
            f"👤 **Tu Perfil Actual**\n\n"
            f"• **Alias:** {alias}\n"
            f"• **Ciudad:** {ciudad}\n"
            f"• **Estado Civil:** {estado_civil}\n"
            f"• **Búsca:** {busca_genero}\n"
            f"• **Fotos subidas:** {num_fotos}/6\n"
            f"• **Descripción:** {descripcion}\n"
            f"• **Estado Visible:** {'Sí (Activo)' if visible else 'No (Oculto)'}"
        )
        kb = [
            [InlineKeyboardButton("📸 Subir Foto (Máx 6)", callback_data="perfil_subir_foto")],
            [InlineKeyboardButton("✏️ Cambiar Descripción", callback_data="perfil_cambiar_desc")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]
        ]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "perfil_subir_foto":
        if perfil and len(perfil[6]) >= 6:
            await query.answer("⚠️ Ya has alcanzado el límite máximo de 6 fotos.", show_alert=True)
            return
        USER_STATES[chat_id] = "SUBIENDO_FOTO"
        kb = [[InlineKeyboardButton("⬅️ Volver a Perfil", callback_data="menu_perfil")]]
        await query.edit_message_text("📸 Envía una foto (en formato imagen) para añadirla a tu perfil (hasta 6 fotos).", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "perfil_cambiar_desc":
        USER_STATES[chat_id] = "CAMBIANDO_DESC"
        kb = [[InlineKeyboardButton("⬅️ Volver a Perfil", callback_data="menu_perfil")]]
        await query.edit_message_text("✏️ Escribe tu nueva descripción o biografía:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "menu_opciones":
        visible_actual = perfil[7] if perfil else True
        texto_vis = "Ocultar Perfil (Modo Invisible)" if visible_actual else "Hacer Visible mi Perfil"
        kb = [
            [InlineKeyboardButton(texto_vis, callback_data="toggle_visibilidad")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]
        ]
        await query.edit_message_text("⚙️ **Opciones de Cuenta**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "toggle_visibilidad":
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("UPDATE perfiles_citas SET visible = NOT visible WHERE chat_id = %s RETURNING visible;", (chat_id,))
            nuevo_estado = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            estado_txt = "visible" if nuevo_estado else "oculto (tus datos ya no aparecen en Descubrir)"
            await query.answer(f"✅ Tu perfil ahora está {estado_txt}.", show_alert=True)
            await query.edit_message_text("⚙️ **Opciones de Cuenta** actualizada.", parse_mode="Markdown", reply_markup=teclado_principal(True, es_admin))
        except Exception as e:
            await query.answer(f"❌ Error: {e}", show_alert=True)

    elif query.data == "menu_descubrir":
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                SELECT chat_id, alias, ciudad, descripcion, estado_civil, fotos 
                FROM perfiles_citas 
                WHERE chat_id != %s AND visible = TRUE 
                ORDER BY RANDOM() LIMIT 1;
            """, (chat_id,))
            match = cur.fetchone()
            cur.close()
            conn.close()

            if not match:
                kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]]
                await query.edit_message_text("🔍 No hay más perfiles disponibles en este momento. ¡Vuelve más tarde!", reply_markup=InlineKeyboardMarkup(kb))
                return

            m_id, m_alias, m_ciudad, m_desc, m_estado, m_fotos = match
            foto_principal = m_fotos[0] if m_fotos and len(m_fotos) > 0 else None

            texto_match = (
                f"🔥 **{m_alias}**, de {m_ciudad}\n"
                f"📌 *Estado:* {m_estado}\n\n"
                f"💬 *Descripción:* {m_desc}"
            )
            kb = [
                [
                    InlineKeyboardButton("❤️ Like", callback_data=f"accion_like_{m_id}"),
                    InlineKeyboardButton("💌 Mensaje", callback_data=f"accion_msg_{m_id}")
                ],
                [InlineKeyboardButton("⏭️ Siguiente Perfil", callback_data="menu_descubrir")],
                [InlineKeyboardButton("⬅️ Menú Principal", callback_data="menu_principal")]
            ]

            if foto_principal:
                await query.message.reply_photo(photo=foto_principal, caption=texto_match, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
                await query.message.delete()
            else:
                await query.edit_message_text(texto_match, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

        except Exception as e:
            await query.edit_message_text(f"❌ Error al buscar perfiles: {e}", reply_markup=teclado_principal(registrado, es_admin))

    elif query.data.startswith("accion_like_"):
        otro_id = int(query.data.split("_")[2])
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("INSERT INTO likes_citas (de_chat_id, para_chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;", (chat_id, otro_id))
            cur.execute("SELECT id FROM likes_citas WHERE de_chat_id = %s AND para_chat_id = %s;", (otro_id, chat_id))
            match_mutuo = cur.fetchone()
            if match_mutuo:
                cur.execute("INSERT INTO conexiones_citas (usuario1, usuario2) VALUES (%s, %s) ON CONFLICT DO NOTHING;", (min(chat_id, otro_id), max(chat_id, otro_id)))
            conn.commit()
            cur.close()
            conn.close()

            await query.answer("❤️ ¡Like enviado con éxito!", show_alert=True)
            if match_mutuo:
                await context.bot.send_message(chat_id=otro_id, text="💖 ¡Has tenido una nueva **Conexión (Match)** mutua! Revisa tu buzón.")
        except Exception as e:
            await query.answer(f"Aviso: {e}", show_alert=True)

    elif query.data.startswith("accion_msg_"):
        otro_id = int(query.data.split("_")[2])
        USER_STATES[chat_id] = {"estado": "ENVIANDO_MENSAJE", "para": otro_id}
        kb = [[InlineKeyboardButton("⬅️ Cancelar", callback_data="menu_descubrir")]]
        await query.message.reply_text("💌 Escribe el mensaje que deseas enviarle a esta persona:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "menu_buzon":
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM likes_citas WHERE para_chat_id = %s;", (chat_id,))
            num_likes = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM conexiones_citas WHERE usuario1 = %s OR usuario2 = %s;", (chat_id, chat_id))
            num_conexiones = cur.fetchone()[0]
            cur.close()
            conn.close()

            msg = (
                f"📬 **Tu Buzón de Actividad**\n\n"
                f"❤️ Has recibido **{num_likes}** Likes.\n"
                f"🔥 Tienes **{num_conexiones}** Conexiones (Matches) activas."
            )
            kb = [
                [InlineKeyboardButton("🔥 Ver Conexiones / Chats", callback_data="ver_conexiones")],
                [InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]
            ]
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error al consultar buzón: {e}", reply_markup=teclado_principal(registrado, es_admin))

    elif query.data == "ver_conexiones":
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                SELECT CASE WHEN usuario1 = %s THEN usuario2 ELSE usuario1 END AS otro_u 
                FROM conexiones_citas WHERE usuario1 = %s OR usuario2 = %s;
            """, (chat_id, chat_id, chat_id))
            conexiones = cur.fetchall()
            cur.close()
            conn.close()

            if not conexiones:
                kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_buzon")]]
                await query.edit_message_text("💬 Aún no tienes conexiones mutuas.", reply_markup=InlineKeyboardMarkup(kb))
                return

            msg = "🔥 **Tus Conexiones Mutuas:**\n\n"
            kb = []
            for c in conexiones:
                otro_id = c[0]
                p_otro = obtener_perfil(otro_id)
                alias_o = p_otro[0] if p_otro else f"Usuario {otro_id}"
                msg += f"• {alias_o}\n"
                kb.append([InlineKeyboardButton(f"💬 Escribir a {alias_o}", callback_data=f"accion_msg_{otro_id}")])
            
            kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_buzon")])
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=teclado_principal(registrado, es_admin))

# --- 5. MANEJADOR DE MENSAJES DE TEXTO Y FOTOS ---
async def manejar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    estado = USER_STATES.get(chat_id)

    if isinstance(estado, dict) and estado.get("estado") == "ENVIANDO_MENSAJE":
        para_id = estado.get("para")
        texto_msg = update.message.text
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("INSERT INTO mensajes_citas (de_chat_id, para_chat_id, mensaje) VALUES (%s, %s, %s);", (chat_id, para_id, texto_msg))
            conn.commit()
            cur.close()
            conn.close()

            USER_STATES.pop(chat_id, None)
            await update.message.reply_text("💌 ¡Mensaje enviado con éxito!")
            await context.bot.send_message(chat_id=para_id, text=f"💌 ¡Has recibido un nuevo mensaje:\n\n\"{texto_msg}\"")
        except Exception as e:
            await update.message.reply_text(f"❌ Error al enviar mensaje: {e}")
        return

    if not estado:
        return

    if estado == "REG_ALIAS":
        alias = update.message.text
        context.user_data["reg_alias"] = alias
        USER_STATES[chat_id] = "REG_CIUDAD"
        await update.message.reply_text("🏙️ ¡Perfecto! Ahora dime, ¿en qué **ciudad** resides?")

    elif estado == "REG_CIUDAD":
        ciudad = update.message.text
        context.user_data["reg_ciudad"] = ciudad
        USER_STATES[chat_id] = "REG_DESC"
        await update.message.reply_text("📝 Escribe una breve **descripción** sobre ti para tu perfil:")

    elif estado == "REG_DESC":
        descripcion = update.message.text
        alias = context.user_data.get("reg_alias", "Usuario")
        ciudad = context.user_data.get("reg_ciudad", "España")

        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO perfiles_citas (chat_id, nombre_telegram, alias, ciudad, descripcion, fotos) 
                VALUES (%s, %s, %s, %s, %s, ARRAY[]::TEXT[]) 
                ON CONFLICT (chat_id) DO UPDATE SET 
                    alias = EXCLUDED.alias,
                    ciudad = EXCLUDED.ciudad,
                    descripcion = EXCLUDED.descripcion,
                    visible = TRUE;
            """, (chat_id, user.first_name, alias, ciudad, descripcion))
            conn.commit()
            cur.close()
            conn.close()

            USER_STATES.pop(chat_id, None)
            await update.message.reply_text(
                "🎉 ¡Registro completado con éxito!\nYa puedes empezar a descubrir perfiles y configurar más fotos en tu menú.",
                reply_markup=teclado_principal(registrado=True, es_admin=(MI_TELEGRAM_ID is not None and chat_id == MI_TELEGRAM_ID))
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error al guardar en base de datos: {e}")

    elif estado == "SUBIENDO_FOTO":
        if update.message.photo:
            foto_file_id = update.message.photo[-1].file_id
            try:
                conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
                cur = conn.cursor()
                cur.execute("UPDATE perfiles_citas SET fotos = array_append(fotos, %s) WHERE chat_id = %s;", (foto_file_id, chat_id))
                conn.commit()
                cur.close()
                conn.close()

                USER_STATES.pop(chat_id, None)
                await update.message.reply_text("✅ ¡Foto añadida correctamente a tu perfil!", reply_markup=teclado_principal(registrado=True, es_admin=(MI_TELEGRAM_ID is not None and chat_id == MI_TELEGRAM_ID)))
            except Exception as e:
                await update.message.reply_text(f"❌ Error al guardar foto: {e}")
        else:
            await update.message.reply_text("⚠️ Por favor, envía una imagen válida.")

    elif estado == "CAMBIANDO_DESC":
        nueva_desc = update.message.text
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("UPDATE perfiles_citas SET descripcion = %s WHERE chat_id = %s;", (nueva_desc, chat_id))
            conn.commit()
            cur.close()
            conn.close()

            USER_STATES.pop(chat_id, None)
            await update.message.reply_text("✅ ¡Descripción actualizada con éxito!", reply_markup=teclado_principal(registrado=True, es_admin=(MI_TELEGRAM_ID is not None and chat_id == MI_TELEGRAM_ID)))
        except Exception as e:
            await update.message.reply_text(f"❌ Error al actualizar: {e}")

# --- 6. FUNCIÓN PRINCIPAL ---
def main():
    logger.info("Iniciando CitasTelegram_Bot...")
    init_db()

    threading.Thread(target=start_health_check, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    if not TOKEN:
        logger.error("❌ TELEGRAM_TOKEN no configurado.")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(procesar_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensajes))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_mensajes))

    app.run_polling()

if __name__ == "__main__":
    main()