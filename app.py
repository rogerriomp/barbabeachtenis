import os
import json
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from datetime import datetime
from dotenv import load_dotenv
from contextlib import contextmanager

from gevent import monkey
monkey.patch_all()

from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chave_super_secreta_barba_beach_2026')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

db_pool = None
TORNEIO_CACHE = None


def init_db():
    global db_pool
    try:
        if db_pool is None and DATABASE_URL:
            db_pool = ThreadedConnectionPool(1, 10, DATABASE_URL)

        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS torneios (
                        id SERIAL PRIMARY KEY,
                        nome VARCHAR(255),
                        categoria VARCHAR(100),
                        formato VARCHAR(100),
                        data_criacao VARCHAR(50),
                        campeao VARCHAR(255),
                        jogo_atual_index INTEGER DEFAULT 0,
                        dados_json JSONB
                    );
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS administradores (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) UNIQUE NOT NULL
                    );
                ''')
                cursor.execute('''
                    INSERT INTO administradores (email) 
                    VALUES ('rogerriomp@gmail.com')
                    ON CONFLICT (email) DO NOTHING;
                ''')
                conn.commit()
        print("-> [Neon PostgreSQL] Conexão e tabelas inicializadas com sucesso!")
        carregar_torneio_cache()
    except Exception as e:
        print(f"-> [Erro PostgreSQL] Falha ao inicializar o banco: {e}")


@contextmanager
def get_db():
    global db_pool
    if db_pool is None and DATABASE_URL:
        db_pool = ThreadedConnectionPool(1, 10, DATABASE_URL)
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)


def carregar_torneio_cache():
    global TORNEIO_CACHE
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT * FROM torneios ORDER BY id DESC LIMIT 1')
                row = cursor.fetchone()
                if row:
                    dados = row['dados_json']
                    torneio = dados if isinstance(dados, dict) else json.loads(dados)
                    torneio['idDb'] = row['id']
                    torneio['dataCriacao'] = row['data_criacao'] or ''
                    torneio['campeao'] = row['campeao'] or 'Em Andamento'
                    TORNEIO_CACHE = torneio
                    return TORNEIO_CACHE
    except Exception as e:
        print(f"Erro ao carregar cache: {e}")
    return None


def salvar_torneio_db_async(torneio_data):
    try:
        torneio_id = torneio_data.get('idDb')
        if not torneio_id:
            return

        status_torneio = torneio_data.get('statusTorneio', 'Em Andamento')

        if status_torneio in ['Cancelado', 'Torneio Cancelado']:
            campeao = 'Torneio Cancelado'
        else:
            campeao = torneio_data.get('campeao', 'Em Andamento')
            jogos = torneio_data.get('jogos', [])
            final_match = next((j for j in jogos if j.get('fase') == 'Grande Final'), None)

            if final_match and final_match.get('vencedor'):
                campeao = final_match['vencedor']
                torneio_data['statusTorneio'] = 'Finalizado'
            else:
                todos_finalizados = len(jogos) > 0 and all(j.get('status') == 'finalizado' for j in jogos)
                if todos_finalizados:
                    torneio_data['statusTorneio'] = 'Finalizado'
                    campeao = jogos[-1].get('vencedor', 'Finalizado')

        json_str = json.dumps(torneio_data)

        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    UPDATE torneios
                    SET dados_json = %s, jogo_atual_index = %s, campeao = %s
                    WHERE id = %s
                ''', (json_str, torneio_data.get('jogoAtualIndex', 0), campeao, torneio_id))
                conn.commit()
    except Exception as e:
        print(f"Erro no salvamento assíncrono: {e}")


init_db()


def usuario_is_admin(email=None):
    if not email:
        email = session.get('user', {}).get('email')
    if not email:
        return False

    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT id FROM administradores WHERE LOWER(email) = LOWER(%s)', (email,))
                row = cursor.fetchone()
                return row is not None
    except Exception:
        return False


@app.route('/')
def index():
    user = session.get('user', None)
    is_admin = usuario_is_admin()
    return render_template('index.html', is_admin=is_admin, user=user, google_client_id=GOOGLE_CLIENT_ID)


@app.route('/api/login-google', methods=['POST'])
def login_google():
    token = request.json.get('token')
    try:
        id_info = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = id_info.get('email')
        nome = id_info.get('name')
        foto = id_info.get('picture')

        is_admin = usuario_is_admin(email)

        session['user'] = {'nome': nome, 'email': email, 'foto': foto}
        session['is_admin'] = is_admin

        return jsonify({'success': True, 'isAdmin': is_admin, 'user': session['user']})
    except Exception as e:
        return jsonify({'error': 'Token inválido', 'details': str(e)}), 400


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/admins', methods=['GET'])
def get_admins():
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT id, email FROM administradores ORDER BY id ASC')
            rows = cursor.fetchall()
            admins = [{'id': r['id'], 'email': r['email']} for r in rows]
            return jsonify(admins)


@app.route('/api/admins', methods=['POST'])
def add_admin():
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    novo_email = request.json.get('email', '').strip().lower()
    if not novo_email:
        return jsonify({'error': 'E-mail inválido'}), 400

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute('INSERT INTO administradores (email) VALUES (%s)', (novo_email,))
                conn.commit()
                return jsonify({'success': True})
    except psycopg2.IntegrityError:
        return jsonify({'error': 'Este e-mail já é um Administrador'}), 400


@app.route('/api/admins/<int:admin_id>', methods=['DELETE'])
def remove_admin(admin_id):
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute('DELETE FROM administradores WHERE id = %s', (admin_id,))
            conn.commit()
            return jsonify({'success': True})


@app.route('/api/torneio', methods=['GET'])
def get_torneio():
    global TORNEIO_CACHE
    torneio_id = request.args.get('id')

    if not torneio_id and TORNEIO_CACHE:
        return jsonify(TORNEIO_CACHE)

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            if torneio_id:
                cursor.execute('SELECT * FROM torneios WHERE id = %s', (torneio_id,))
            else:
                cursor.execute('SELECT * FROM torneios ORDER BY id DESC LIMIT 1')
            row = cursor.fetchone()

    if not row:
        return jsonify(None)

    try:
        dados = row['dados_json']
        torneio = dados if isinstance(dados, dict) else json.loads(dados)
        torneio['idDb'] = row['id']
        torneio['dataCriacao'] = row['data_criacao'] or ''
        torneio['campeao'] = row['campeao'] or 'Em Andamento'
        if not torneio_id:
            TORNEIO_CACHE = torneio
        return jsonify(torneio)
    except Exception:
        return jsonify(None)


@app.route('/api/historico', methods=['GET'])
def get_historico():
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT id, nome, categoria, data_criacao, campeao FROM torneios ORDER BY id DESC')
                rows = cursor.fetchall()

        historico = []
        for r in rows:
            historico.append({
                'id': r['id'],
                'nome': r['nome'] or 'Torneio',
                'categoria': r['categoria'] or 'Geral',
                'data': r['data_criacao'] or '',
                'campeao': r['campeao'] if r['campeao'] else 'Em Andamento'
            })
        return jsonify(historico)
    except Exception:
        return jsonify([])


@app.route('/api/torneio', methods=['POST'])
def create_torneio():
    global TORNEIO_CACHE
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('''
                SELECT id, nome FROM torneios 
                WHERE campeao = 'Em Andamento' 
                ORDER BY id DESC LIMIT 1
            ''')
            torneio_ativo = cursor.fetchone()

            if torneio_ativo:
                return jsonify({
                    'error': f"Já existe um torneio em andamento ('{torneio_ativo['nome']}'). Finalize ou cancele ele antes de criar um novo!"
                }), 400

    torneio_data = request.json
    data_atual = datetime.now().strftime("%d/%m/%Y %H:%M")
    torneio_data['dataCriacao'] = data_atual
    torneio_data['statusTorneio'] = 'Em Andamento'

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            json_str = json.dumps(torneio_data)
            cursor.execute('''
                INSERT INTO torneios (nome, categoria, formato, data_criacao, campeao, jogo_atual_index, dados_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            ''', (
                torneio_data.get('nome'),
                torneio_data.get('categoria'),
                torneio_data.get('formato'),
                data_atual,
                "Em Andamento",
                torneio_data.get('jogoAtualIndex', 0),
                json_str
            ))
            novo_id = cursor.fetchone()['id']
            conn.commit()

    torneio_data['idDb'] = novo_id
    TORNEIO_CACHE = torneio_data
    socketio.emit('torneio_atualizado', TORNEIO_CACHE)
    return jsonify(TORNEIO_CACHE)


@app.route('/api/torneio/<int:torneio_id>', methods=['PUT'])
def update_torneio(torneio_id):
    global TORNEIO_CACHE
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    torneio_data = request.json
    TORNEIO_CACHE = torneio_data

    socketio.emit('torneio_atualizado', TORNEIO_CACHE)
    socketio.start_background_task(salvar_torneio_db_async, torneio_data)
    return jsonify({'success': True})


@app.route('/api/torneio/<int:torneio_id>/cancelar', methods=['PUT'])
def cancelar_torneio(torneio_id):
    global TORNEIO_CACHE
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT dados_json FROM torneios WHERE id = %s', (torneio_id,))
            row = cursor.fetchone()
            if not row:
                return jsonify({'error': 'Torneio não encontrado'}), 404

            dados = row['dados_json'] if isinstance(row['dados_json'], dict) else json.loads(row['dados_json'])
            dados['statusTorneio'] = 'Cancelado'
            dados['campeao'] = 'Torneio Cancelado'
            dados['idDb'] = torneio_id
            json_str = json.dumps(dados)

            cursor.execute('''
                UPDATE torneios 
                SET campeao = 'Torneio Cancelado', dados_json = %s 
                WHERE id = %s
            ''', (json_str, torneio_id))
            conn.commit()

    TORNEIO_CACHE = dados
    socketio.emit('torneio_atualizado', TORNEIO_CACHE)
    return jsonify({'success': True, 'message': 'Torneio cancelado com sucesso'})


@socketio.on('atualizar_torneio')
def handle_atualizar_torneio(data):
    global TORNEIO_CACHE
    TORNEIO_CACHE = data
    socketio.emit('torneio_atualizado', TORNEIO_CACHE)
    socketio.start_background_task(salvar_torneio_db_async, data)


@socketio.on('connect')
def handle_connect():
    global TORNEIO_CACHE
    if TORNEIO_CACHE:
        emit('torneio_atualizado', TORNEIO_CACHE)
    else:
        carregar_torneio_cache()
        if TORNEIO_CACHE:
            emit('torneio_atualizado', TORNEIO_CACHE)


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)