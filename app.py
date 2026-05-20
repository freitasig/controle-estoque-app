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
# CONFIGURAÇÃO DA PÁGINA E CSS RESPONSIVO SEGURO
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="WMS 4.0 - Alta Performance", page_icon="📦", layout="wide")

st.markdown("""
    <style>
    /* Alinha os botões principais */
    .stButton>button {
        border-radius: 10px;
        font-weight: 600;
        height: 3em;
        width: 100%;
        margin-top: 10px;
    }
    /* Cartões de métricas transparentes para herdar o tema do config.toml */
    .metric-card {
        padding: 20px;
        border-radius: 12px;
        border-top: 4px solid #0052cc;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        margin-bottom: 15px;
    }
    .stNumberInput, .stTextInput, .stSelectbox {
        margin-bottom: 10px;
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

# ─── VARIÁVEIS GLOBAIS FIXADAS (RESOLVE OS 12 ERROS DO SEU PRINT) ───
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
    st.session_state["db_sincronizado"] = True

# ─────────────────────────────────────────────────────────────
# INTERFACE PRINCIPAL
# ─────────────────────────────────────────────────────────────
st.title("📦 WMS Inteligente")
st.caption("Controle Operacional Avançado | Performance Otimizada")

aba_painel, aba_operacao, aba_contagem, aba_ia, aba_historico, aba_gestao = st.tabs([
    "📊 Painel", "⚡ Saídas/Entradas", "📋 INVENTÁRIO", "🧠 IA Analista", "📜 Histórico", "⚙️ Config"
])

# PAINEL
with aba_painel:
    df = listar_produtos()
    if not df.empty:
        df["valor_total"] = df["saldo_atual"] * df["valor_unitario"]
        
        # Coleta de consumo histórico baseado nas baixas
        with get_conn() as conn:
            cons = pd.read_sql("""
                SELECT id_produto, SUM(ABS(quantidade)) as total 
                FROM movimentacoes 
                WHERE tipo='Saída' OR (tipo='Contagem' AND quantidade < 0)
                GROUP BY id_produto
            """, conn)
            
        df = df.merge(cons, left_on='id', right_on='id_produto', how='left').fillna(0)
        df['consumo_diario'] = df['total'] / 30
        
        # Cálculo Seguro de Cobertura (Runway)
        mask = df['consumo_diario'] > 0
        df['Runway'] = 999
        df.loc[mask, 'Runway'] = (df.loc[mask, 'saldo_atual'] / df.loc[mask, 'consumo_diario']).astype(int)
        
        def set_status(row):
            if row['saldo_atual'] <= 0: return '🔴 Ruptura'
            if row['saldo_atual'] < row['estoque_minimo']: return '🔴 Crítico'
            if row['Runway'] != 999 and row['Runway'] <= row['lead_time']: return '🟠 Risco'
            return '🟢 OK'
        df['Status'] = df.apply(set_status, axis=1)
        df['Runway'] = df['Runway'].apply(lambda x: "Sem consumo" if x == 999 else f"{x} dias")

        # Layout Responsivo
        c1, c2, c3, c4 = st.columns([1,1,1,1])
        c1.markdown(f'<div class="metric-card">Categorias<br><b>{df["categoria"].nunique()}</b></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card">Valor Total<br><b>R$ {df["valor_total"].sum():,.2f}</b></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card">Itens Críticos<br><b>{(df["saldo_atual"] < df["estoque_minimo"]).sum()}</b></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="metric-card">Giro Total<br><b>{int(df["total"].sum())} un</b></div>', unsafe_allow_html=True)

        st.divider()
        st.subheader("📋 Posição de Estoque")
        st.dataframe(df[['Status', 'categoria', 'nome', 'saldo_atual', 'estoque_minimo', 'Runway']].rename(columns={'categoria':'Setor', 'nome':'Produto'}), hide_index=True, use_container_width=True)

        st.divider()
        st.subheader("🛒 Sugestão de Reposição (Cálculo WMS)")
        df["Minimo Ideal"] = (df["consumo_diario"] * df["lead_time"] * 1.2).astype(int)
        df["Alvo"] = df[["estoque_minimo", "Minimo Ideal"]].max(axis=1)
        df["Sugestão Compra"] = (df["Alvo"] - df["saldo_atual"]).clip(lower=0)
        st.dataframe(df[["categoria", "nome", "lead_time", "saldo_atual", "Minimo Ideal", "Sugestão Compra"]].rename(columns={"categoria": "Setor", "nome": "Produto", "lead_time": "Entrega(d)", "saldo_atual": "Saldo", "Sugestão Compra": "Comprar"}), hide_index=True, use_container_width=True)

# OPERAÇÃO (SAÍDAS E ENTRADAS COM INVERSÃO E OBSERVAÇÕES)
with aba_operacao:
    df = listar_produtos()
    if not df.empty:
        col_e, col_s = st.columns(2) # Entrada Esquerda, Saída Direita
        
        with col_e:
            with st.container(border=True):
                st.subheader("⬇️ Registrar Entrada")
                ops = dict(zip(df["nome"], df["id"]))
                sel_e = st.selectbox("Produto", list(ops.keys()), key="e_p")
                id_pe = ops[sel_e]
                sal_e = int(df.loc[df["id"]==id_pe, "saldo_atual"].values[0])
                
                c1, c2 = st.columns([1, 2])
                with c1: qe = st.number_input("Quantidade", min_value=1, key="e_q")
                with c2: obs_e = st.text_input("Nota/Fornecedor", key="e_obs")
                    
                if st.button("Confirmar Entrada", type="secondary"):
                    with get_conn() as conn:
                        conn.execute("UPDATE produtos SET saldo_atual = saldo_atual + ? WHERE id = ?", (qe, id_pe))
                        data = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
                        conn.execute("INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao) VALUES (?, ?, 'Entrada', ?, ?, ?)", (id_pe, data, qe, sal_e + qe, obs_e))
                    disparar_sincronizacao()
                    st.rerun()

        with col_s:
            with st.container(border=True):
                st.subheader("📤 Registrar Saída")
                sel = st.selectbox("Produto ", list(ops.keys()), key="s_p")
                id_p = ops[sel]
                max_s = int(df.loc[df["id"]==id_p, "saldo_atual"].values[0])
                
                c1, c2 = st.columns([1, 2])
                with c1: q = st.number_input("Quantidade", min_value=1, max_value=max(max_s, 1), key="s_q")
                with c2: obs_s = st.text_input("Observação/Destino", key="s_obs")
                    
                if st.button("Confirmar Saída", type="primary"):
                    with get_conn() as conn:
                        conn.execute("UPDATE produtos SET saldo_atual = saldo_atual - ? WHERE id = ?", (q, id_p))
                        data = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
                        conn.execute("INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao) VALUES (?, ?, 'Saída', ?, ?, ?)", (id_p, data, -q, max_s - q, obs_s))
                    disparar_sincronizacao()
                    st.rerun()

# ABA EXCLUSIVA: INVENTÁRIO / CONTAGEM
with aba_contagem:
    st.subheader("📋 Auditoria de Inventário Semanal")
    st.info("Aba dedicada para auditoria física. O consumo da operação é calculated através destas contagens.")
    df = listar_produtos()
    if not df.empty:
        with st.container(border=True):
            ops = dict(zip(df["nome"], df["id"]))
            sel_c = st.selectbox("Selecione o Insumo para Contagem", list(ops.keys()), key="c_p")
            id_pc = ops[sel_c]
            s_sis = int(df.loc[df["id"]==id_pc, "saldo_atual"].values[0])
            
            st.metric("Saldo Atual no Sistema", f"{s_sis} un")
            f_cont = st.number_input("Quantidade Física Contada", min_value=0, step=1, key="c_q")
            
            diff = f_cont - s_sis
            if diff == 0: st.success("✅ Saldo bate perfeitamente com o sistema.")
            elif diff < 0: st.warning(f"📉 Baixa/Consumo detectado: {abs(diff)} unidades utilizadas.")
            else: st.info(f"📈 Ajuste positivo: {diff} unidades encontradas.")
            
            if st.button("💾 Gravar e Sincronizar Inventário", use_container_width=True, type="primary"):
                with get_conn() as conn:
                    conn.execute("UPDATE produtos SET saldo_atual = ? WHERE id = ?", (f_cont, id_pc))
                    data = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
                    conn.execute("INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao) VALUES (?, ?, 'Contagem', ?, ?, 'Inventário Semanal')", (id_pc, data, diff, f_cont))
                disparar_sincronizacao()
                st.toast("Inventário Sincronizado!", icon="✅")
                st.rerun()

# IA ANALISTA (COM HISTÓRICO DE CONSUMO REAL)
with aba_ia:
    st.subheader("🧠 Assistente IA de Suprimentos")
    if st.button("✨ Gerar Diagnóstico Logístico"):
        df = listar_produtos()
        if not df.empty:
            with st.spinner("Analisando dados com contexto de consumo..."):
                try:
                    with get_conn() as conn:
                        cons = pd.read_sql("""
                            SELECT id_produto, SUM(ABS(quantidade)) as consumo_total 
                            FROM movimentacoes 
                            WHERE tipo='Saída' OR (tipo='Contagem' AND quantidade < 0) 
                            GROUP BY id_produto
                        """, conn)
                    df = df.merge(cons, left_on='id', right_on='id_produto', how='left').fillna(0)
                    df['consumo_mensal'] = df['consumo_total'].astype(int)

                    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                    modelos = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
                    mod_name = next((m for m in modelos if 'flash' in m or 'pro' in m), modelos[0])
                    mod = genai.GenerativeModel(mod_name)
                    
                    dados_para_ia = df[['categoria', 'nome', 'saldo_atual', 'estoque_minimo', 'lead_time', 'consumo_mensal']].to_string(index=False)
                    prompt = f"Analise o estoque logístico (lead_time em DIAS, consumo_mensal real):\n{dados_para_ia}\nEntregue: Resumo de saúde, riscos de ruptura antes do lead time e sugestão de compras."
                    st.write(mod.generate_content(prompt).text)
                except Exception as e: st.error(f"Erro IA: {e}")

# OPTIMIZAÇÃO 3: FILTRO DE CARGA NO HISTÓRICO (PAGINAÇÃO MENSAL)
with aba_historico:
    st.subheader("📜 Histórico de Movimentações")
    mv = listar_movimentacoes()
    if not mv.empty:
        mv['Mês/Ano'] = mv['data_hora'].apply(lambda x: x.split()[0][3:])
        meses_disponiveis = sorted(mv['Mês/Ano'].unique(), reverse=True)
        
        c_filt, _ = st.columns([2, 6])
        with c_filt:
            mes_selecionado = st.selectbox("Filtrar Histórico por Período:", meses_disponiveis)
        
        mv_filtrado = mv[mv['Mês/Ano'] == mes_selecionado].drop(columns=['Mês/Ano'])
        
        st.dataframe(mv_filtrado, use_container_width=True, hide_index=True)
        st.download_button("📥 Baixar Dados do Mês (CSV)", mv_filtrado.to_csv(index=False).encode('utf-8-sig'), f"historico_{mes_selecionado.replace('/', '_')}.csv")
    else:
        st.info("Nenhuma movimentação registrada.")

# GESTÃO DE PRODUTOS (CADASTRAR / EDITAR / EXCLUIR SEGURA EM 2 ETAPAS)
with aba_gestao:
    a1, a2, a3 = st.tabs(["➕ Novo", "✏️ Editar", "🗑️ Excluir"])
    
    with a1:
        with st.form("new_p"):
            n = st.text_input("Nome do Insumo")
            c = st.selectbox("Setor", ["Limpeza", "Copa", "EPI", "Escritório", "Geral"])
            m = st.number_input("Mínimo", value=10)
            l = st.number_input("Lead Time (Dias)", value=3)
            v = st.number_input("Valor Un.", value=0.0)
            if st.form_submit_button("Cadastrar"):
                if n.strip():
                    cadastrar_produto(n.strip(), m, v, c, l)
                    disparar_sincronizacao()
                    st.rerun()
                
    with a2:
        df = listar_produtos()
        if not df.empty:
            op_e = dict(zip(df["nome"], df["id"]))
            s_e = st.selectbox("Produto p/ Editar", list(op_e.keys()))
            id_e = op_e[s_e]
            p_at = df[df["id"]==id_e].iloc[0]
            with st.form("edit_p"):
                en = st.text_input("Nome", value=p_at["nome"])
                ec = st.selectbox("Setor", ["Limpeza", "Copa", "EPI", "Escritório", "Geral"], index=0)
                em = st.number_input("Mínimo", value=int(p_at["estoque_minimo"]))
                el = st.number_input("Lead Time", value=int(p_at["lead_time"]))
                ev = st.number_input("Valor Un.", value=float(p_at["valor_unitario"]))
                if st.form_submit_button("Atualizar"):
                    editar_produto(id_e, en, em, ev, ec, el)
                    disparar_sincronizacao()
                    st.rerun()
                    
    with a3:
        st.subheader("🗑️ Eliminar Insumo da Base")
        df = listar_produtos()
        if not df.empty:
            op_d = dict(zip(df["nome"], df["id"]))
            s_d = st.selectbox("Selecione o Insumo para Excluir", list(op_d.keys()), key="del_select")
            id_d = op_d[s_d]
            
            st.warning(f"⚠️ **Aviso de Integridade:** Eliminar o item '{s_d}' irá apagar permanentemente o seu registo do cadastro e **destruirá todo o histórico de movimentações** associado. Esta ação não pode ser desfeita.")
            
            confirmar_exclusao = st.checkbox("Confirmo que verifiquei os dados e pretendo apagar este insumo e o seu histórico definitivamente.", key="del_check")
            
            if st.button("🗑️ Eliminar Definitivamente", type="primary", disabled=not confirmar_exclusao, key="del_btn"):
                deletar_produto(id_d)
                disparar_sincronizacao()
                st.toast(f"'{s_d}' foi removido do sistema.", icon="🗑️")
                st.rerun()
        else:
            st.info("Nenhum produto cadastrado para exclusão.")