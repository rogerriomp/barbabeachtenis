import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
from dotenv import load_dotenv  # <- IMPORTA A BIBLIOTECA
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# Carrega as variáveis de ambiente do arquivo .env no computador local
load_dotenv()

app = Flask(__name__)

# Lê as variáveis com valores padrão de segurança/fallback
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chave_padrao_desenvolvimento')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

socketio = SocketIO(app, cors_allowed_origins="*")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                # Tabela de Torneios com suporte a JSONB nativo do Postgres
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
                # Tabela de Administradores
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS administradores (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) UNIQUE NOT NULL
                    );
                ''')
                # Inserção do administrador padrão se ainda não existir
                cursor.execute('''
                    INSERT INTO administradores (email) 
                    VALUES ('rogerriomp@gmail.com')
                    ON CONFLICT (email) DO NOTHING;
                ''')
                conn.commit()
        print("-> [Neon PostgreSQL] Conexão e tabelas inicializadas com sucesso!")
    except Exception as e:
        print(f"-> [Erro PostgreSQL] Falha ao inicializar o banco: {e}")


init_db()


def usuario_is_admin(email=None):
    if not email:
        email = session.get('user', {}).get('email')
    if not email:
        return False

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
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
        with conn.cursor() as cursor:
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
    torneio_id = request.args.get('id')
    with get_db() as conn:
        with conn.cursor() as cursor:
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
        return jsonify(torneio)
    except Exception:
        return jsonify(None)


@app.route('/api/historico', methods=['GET'])
def get_historico():
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
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
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    torneio_data = request.json
    data_atual = datetime.now().strftime("%d/%m/%Y %H:%M")
    torneio_data['dataCriacao'] = data_atual
    torneio_data['statusTorneio'] = 'Em Andamento'

    with get_db() as conn:
        with conn.cursor() as cursor:
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
    socketio.emit('torneio_atualizado', torneio_data)
    return jsonify(torneio_data)


@app.route('/api/torneio/<int:torneio_id>', methods=['PUT'])
def update_torneio(torneio_id):
    if not usuario_is_admin():
        return jsonify({'error': 'Acesso negado'}), 403

    torneio_data = request.json

    campeao = "Em Andamento"
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

    socketio.emit('torneio_atualizado', torneio_data)
    return jsonify({'success': True})


@socketio.on('connect')
def handle_connect():
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT * FROM torneios ORDER BY id DESC LIMIT 1')
                row = cursor.fetchone()
                if row:
                    dados = row['dados_json']
                    torneio = dados if isinstance(dados, dict) else json.loads(dados)
                    torneio['idDb'] = row['id']
                    torneio['dataCriacao'] = row['data_criacao'] or ''
                    torneio['campeao'] = row['campeao'] or 'Em Andamento'
                    emit('torneio_atualizado', torneio)
    except Exception:
        pass


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)