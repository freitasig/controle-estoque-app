import streamlit as st
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from io import BytesIO
import os
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
import numpy as np
import threading

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÃO DA PÁGINA E CSS RESPONSIVO FORÇADO MODO ESCURO
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="WMS 4.0 - Alta Performance", page_icon="📦", layout="wide")

# CSS Avançado que força as cores do Modo Escuro diretamente na página
st.markdown("""
    <style>
    /* Força o fundo escuro da página inteira e a cor do texto */
    .stApp {
        background-color: #0f172a !important;
        color: #f8fafc !important;
    }
    /* Alinha os botões principais */
    .stButton>button {
        border-radius: 10px;
        font-weight: 600;
        height: 3em;
        width: 100%;
        margin-top: 10px;
    }
    /* Cartões de métricas no modo escuro */
    .metric-card {
        background-color: #1e293b !important;
        color: #f8fafc !important;
        padding: 20px;
        border-radius: 12px;
        border-top: 4px solid #0052cc;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        margin-bottom: 15px;
    }
    /* Força os títulos das abas a ficarem legíveis no escuro */
    .stTabs [data-baseweb="tab"] {
        color: #94a3b8 !important;
    }
    .stTabs [aria-selected="true"] {
        color: #f8fafc !important;
        font-weight: bold;
    }
    /* Inputs de texto e números legíveis */
    .stNumberInput, .stTextInput, .stSelectbox {
        margin-bottom: 10px;
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# ─── VARIÁVEIS GLOBAIS ───
DB_PATH = "estoque.db"
FOLDER_ID = st.secrets["FOLDER_ID"]

# ─────────────────────────────────────────────────────────────
# OPTIMIZAÇÃO 1: SINCRONIZAÇÃO EM SEGUNDO PLANO (THREADING ASYNC)
# ─────────────────────────────────────────────────────────────
def executar_sincronizacao_drive():
    """Roda de forma invisível em segundo plano sem congelar a interface do utilizador."""
    try:
        servico = obter_servico_drive()
        query = f"name='{DB_PATH}' and '{FOLDER_ID}' in parents and trashed=false"
        files = servico.files().list(q=query, fields="files(id)").execute().get('files', [])
        media = MediaFileUpload(DB_PATH, mimetype='application/x-sqlite3', resumable=True)
        if files: 
            servico.files().update(fileId=files[0]['id'], media_body=media).execute()
        else: 
            servico.files().create(body={'name': DB_PATH, 'parents': [FOLDER_ID]}, media_body=media).execute()
        
        # Gera os espelhos CSV para o Looker Studio nos bastidores
        with sqlite3.connect(DB_PATH) as conn:
            prods = pd.read_sql("SELECT * FROM produtos ORDER BY nome", conn)
            movs = pd.read_sql("""
                SELECT m.id, p.nome AS produto, m.data_hora, m.tipo, m.quantidade, m.saldo_resultante, m.observacao
                FROM movimentacoes m JOIN produtos p ON p.id = m.id_produto ORDER BY m.id DESC
            """, conn)
            
        for df, name in [(prods, "produtos_looker.csv"), (movs, "movimentacoes_looker.csv")]:
            q = f"name='{name}' and '{FOLDER_ID}' in parents"
            fs = servico.files().list(q=q).execute().get('files', [])
            m = MediaIoBaseUpload(BytesIO(df.to_csv(index=False).encode('utf-8-sig')), mimetype='text/csv')
            if fs: servico.files().update(fileId=fs[0]['id'], media_body=m).execute()
            else: servico.files().create(body={'name': name, 'parents': [FOLDER_ID]}, media_body=m).execute()
    except:
        pass

def disparar_sincronizacao():
    """Limpa o cache local instantaneamente e delega o upload à nuvem para outra Thread."""
    st.cache_data.clear()
    threading.Thread(target=executar_sincronizacao_drive).start()

def obter_servico_drive():
    info_chaves = dict(st.secrets["gcp_service_account"])
    credenciais = service_account.Credentials.from_service_account_info(info_chaves)
    return build('drive', 'v3', credentials=credenciais)

def descarregar_do_drive():
    try:
        servico = obter_servico_drive()
        query = f"name='{DB_PATH}' and '{FOLDER_ID}' in parents and trashed=false"
        res = servico.files().list(q=query, fields="files(id)").execute()
        if res.get('files', []):
            req = servico.files().get_media(fileId=res['files'][0]['id'])
            with open(DB_PATH, "wb") as f:
                load = MediaIoBaseDownload(f, req)
                done = False
                while not done: _, done = load.next_chunk()
            return True
    except: return False
    return False

# ─────────────────────────────────────────────────────────────
# OPTIMIZAÇÃO 2: MEMÓRIA EM CACHE (`@st.cache_data`)
# ─────────────────────────────────────────────────────────────
def get_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

@st.cache_data
def listar_produtos():
    with get_conn() as conn: return pd.read_sql("SELECT * FROM produtos ORDER BY nome", conn)

@st.cache_data
def listar_movimentacoes():
    with get_conn() as conn:
        return pd.read_sql("""
            SELECT m.id, p.nome AS produto, m.data_hora, m.tipo, m.quantidade, m.saldo_resultante, m.observacao
            FROM movimentacoes m JOIN produtos p ON p.id = m.id_produto ORDER BY m.id DESC
        """, conn)

def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                saldo_atual INTEGER NOT NULL DEFAULT 0,
                estoque_minimo INTEGER DEFAULT 10,
                valor_unitario REAL DEFAULT 0,
                categoria TEXT DEFAULT 'Geral',
                lead_time INTEGER DEFAULT 3
            );
            CREATE TABLE IF NOT EXISTS movimentacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_produto INTEGER NOT NULL REFERENCES produtos(id),
                data_hora TEXT NOT NULL,
                tipo TEXT NOT NULL,
                quantidade INTEGER NOT NULL,
                saldo_resultante INTEGER NOT NULL,
                observacao TEXT
            );
        """)

def cadastrar_produto(nome, estoque_minimo, valor_unitario, categoria, lead_time):
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO produtos (nome, saldo_atual, estoque_minimo, valor_unitario, categoria, lead_time) VALUES (?, 0, ?, ?, ?, ?)", (nome, estoque_minimo, valor_unitario, categoria, lead_time))
        return True, "Sucesso"
    except: return False, "Erro"

def editar_produto(id_p, nome, min_e, valor, cat, lead):
    with get_conn() as conn:
        conn.execute("UPDATE produtos SET nome=?, estoque_minimo=?, valor_unitario=?, categoria=?, lead_time=? WHERE id=?", (nome, min_e, valor, cat, lead, id_p))

def deletar_produto(id_produto):
    with get_conn() as conn:
        conn.execute("DELETE FROM movimentacoes WHERE id_produto = ?", (id_produto,))
        conn.execute("DELETE FROM produtos WHERE id = ?", (id_produto,))

# ─────────────────────────────────────────────────────────────
# INICIALIZAÇÃO CONTROLADA
# ─────────────────────────────────────────────────────────────
if "db_sincronizado" not in st.session_state:
    descarregar_do_drive()
    init_db()