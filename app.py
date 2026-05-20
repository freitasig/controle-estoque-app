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
    /* Base dos cartões de métricas */
    .metric-card {
        padding: 20px;
        border-radius: 12px;
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

# ─── VARIÁVEIS GLOBAIS ───
DB_PATH = "estoque.db"
FOLDER_ID = st.secrets["FOLDER_ID"]

# ─────────────────────────────────────────────────────────────
# OPTIMIZAÇÃO 1: SINCRONIZAÇÃO EM SEGUNDO PLANO (THREADING ASYNC)
# ─────────────────────────────────────────────────────────────
def executar_sincronizacao_drive():
    try:
        servico = obter_servico_drive()
        query = f"name='{DB_PATH}' and '{FOLDER_ID}' in parents and trashed=false"
        files = servico.files().list(q=query, fields="files(id)").execute().get('files', [])
        media = MediaFileUpload(DB_PATH, mimetype='application/x-sqlite3', resumable=True)
        if files: 
            servico.files().update(fileId=files[0]['id'], media_body=media).execute()
        else: 
            servico.files().create(body={'name': DB_PATH, 'parents': [FOLDER_ID]}, media_body=media).execute()
        
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
        
        with get_conn() as conn:
            cons = pd.read_sql("""
                SELECT id_produto, SUM(ABS(quantidade)) as total 
                FROM movimentacoes 
                WHERE tipo='Saída' OR (tipo='Contagem' AND quantidade < 0)
                GROUP BY id_produto
            """, conn)
            
        df = df.merge(cons, left_on='id', right_on='id_produto', how='left').fillna(0)
        df['consumo_diario'] = df['total'] / 30
        
        mask = df['consumo_diario'] > 0
        df['Runway'] = 999
        df.loc[mask, 'Runway'] = (df.loc[mask, 'saldo_atual'] / df.loc[mask, 'consumo_diario']).astype(int)
        
        def set_status(row):
            if row['saldo_atual'] <= 0: return '🔴 Ruptura'
            if row['saldo_atual'] < row['estoque_minimo']: return '🔴 Crítico'
            if row['Runway'] != 999 and row['Runway'] <= row['lead_time']: return '🟠 Risco'
            return '🟢 OK'
        df['Status'] = df.apply(set_status, axis=1)
        df['Runway_Txt'] = df['Runway'].apply(lambda x: "Sem consumo" if x == 999 else f"{x} dias")

        # CARDS DE MÉTRICAS DINÂMICOS
        itens_criticos = int((df["saldo_atual"] < df["estoque_minimo"]).sum())
        if itens_criticos > 0:
            card_critico_style = 'background-color: rgba(239, 68, 68, 0.15); border-top: 4px solid #ef4444; color: #ef4444;'
        else:
            card_critico_style = 'background-color: rgba(16, 185, 129, 0.15); border-top: 4px solid #10b859; color: #10b859;'

        c1, c2, c3, c4 = st.columns([1,1,1,1])
        c1.markdown(f'<div class="metric-card" style="border-top: 4px solid #0052cc;">Categorias<br><b>{df["categoria"].nunique()}</b></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card" style="border-top: 4px solid #0052cc;">Valor Total<br><b>R$ {df["valor_total"].sum():,.2f}</b></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card" style="{card_critico_style}">Itens Críticos/Ruptura<br><b>{itens_criticos}</b></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="metric-card" style="border-top: 4px solid #0052cc;">Giro Total<br><b>{int(df["total"].sum())} un</b></div>', unsafe_allow_html=True)

        st.divider()
        
        # FILTROS AVANÇADOS DE PESQUISA E SETOR
        cp1, cp2 = st.columns([1, 1])
        with cp1:
            setores = ["Todos"] + list(df["categoria"].unique())
            setor_sel = st.selectbox("⚡ Filtrar por Setor:", setores)
        with cp2:
            busca_nome = st.text_input("🔍 Busca Rápida por Nome do Insumo:")
        
        df_filtrado = df.copy()
        if setor_sel != "Todos":
            df_filtrado = df_filtrado[df_filtrado["categoria"] == setor_sel]
        if busca_nome.strip():
            df_filtrado = df_filtrado[df_filtrado["nome"].str.contains(busca_nome, case=False)]

        st.subheader("📋 Posição de Estoque")
        
        def destacar_status(val):
            if '🔴' in str(val): return 'background-color: rgba(239, 68, 68, 0.35); color: #000000; font-weight: bold;'
            if '🟠' in str(val): return 'background-color: rgba(245, 158, 11, 0.35); color: #000000; font-weight: bold;'
            if '🟢' in str(val): return 'background-color: rgba(16, 185, 129, 0.35); color: #000000; font-weight: bold;'
            return ''

        display_df = df_filtrado[['Status', 'categoria', 'nome', 'saldo_atual', 'valor_unitario', 'estoque_minimo', 'Runway_Txt']].rename(
            columns={'categoria':'Setor', 'nome':'Produto', 'valor_unitario': 'Preço Médio', 'Runway_Txt':'Cobertura (Runway)'}
        )
        
        st.dataframe(
            display_df.style.map(destacar_status, subset=['Status']).format({'Preço Médio': 'R$ {:.2f}'}),
            hide_index=True, width='stretch'
        )

        # GRÁFICOS NATIVOS DE CONSUMO
        st.divider()
        st.subheader("📊 Gráficos de Performance e Movimentação")
        g1, g2 = st.columns(2)
        
        df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)
        
        with g1:
            st.markdown("##### 📊 Giro Total (Saídas) por Categoria")
            giro_setor = df.groupby("categoria")["total"].sum().reset_index().rename(columns={"categoria": "Setor", "total": "Movimentações"})
            if giro_setor["Movimentações"].sum() > 0:
                st.bar_chart(data=giro_setor, x="Setor", y="Movimentações", width='stretch')
            else:
                st.info("Ainda não há registros de saídas para gerar o gráfico de setores.")
                
        with g2:
            st.markdown("##### 🏆 Top 5 Insumos Mais Consumidos (30 dias)")
            df_consumo_real = df[df["total"] > 0]
            if not df_consumo_real.empty:
                top_consumo = df_consumo_real.nlargest(5, "total")[["nome", "total"]].rename(columns={"nome": "Insumo", "total": "Quantidade"})
                st.bar_chart(data=top_consumo, x="Insumo", y="Quantidade", width='stretch')
            else:
                st.info("Ainda não há consumo registrado para listar o ranking.")

        st.divider()
        st.subheader("🛒 Sugestão de Reposição (Cálculo WMS)")
        
        df_filtrado["Minimo Ideal"] = (df_filtrado["consumo_diario"] * df_filtrado["lead_time"] * 1.2).astype(int)
        df_filtrado["Alvo"] = df_filtrado[["estoque_minimo", "Minimo Ideal"]].max(axis=1)
        df_filtrado["Sugestão Compra"] = (df_filtrado["Alvo"] - df_filtrado["saldo_atual"]).clip(lower=0)
        
        apenas_compras = st.checkbox("🛒 Mostrar apenas insumos com necessidade de compra urgente")
        df_compras = df_filtrado.copy()
        if apenas_compras:
            df_compras = df_compras[df_compras["Sugestão Compra"] > 0]
            
        st.dataframe(df_compras[["categoria", "nome", "lead_time", "saldo_atual", "Minimo Ideal", "Sugestão Compra"]].rename(columns={"categoria": "Setor", "nome": "Produto", "lead_time": "Entrega(d)", "saldo_atual": "Saldo", "Sugestão Compra": "Comprar"}), hide_index=True, width='stretch')

# OPERAÇÃO (SAÍDAS E ENTRADAS COM INVERSÃO E OBSERVAÇÕES)
with aba_operacao:
    df = listar_produtos()
    if not df.empty:
        col_e, col_s = st.columns(2)
        
        with col_e:
            with st.container(border=True):
                st.subheader("⬇️ Registrar Entrada")
                ops = dict(zip(df["nome"], df["id"]))
                sel_e = st.selectbox("Produto", list(ops.keys()), key="e_p")
                id_pe = ops[sel_e]
                
                p_atual = df.loc[df["id"]==id_pe].iloc[0]
                sal_e = int(p_atual["saldo_atual"])
                pmp_antigo = float(p_atual["valor_unitario"])
                
                c1, c2 = st.columns([1, 1])
                with c1: qe = st.number_input("Quantidade", min_value=1, key="e_q")
                with c2: preco_compra = st.number_input("Preço Unit. de Compra (R$)", min_value=0.0, value=pmp_antigo, step=0.01, key="e_v")
                
                obs_e = st.text_input("Nota/Fornecedor", key="e_obs")
                    
                if st.button("Confirmar Entrada", type="secondary"):
                    total_novas_unidades = sal_e + qe
                    if total_novas_unidades > 0:
                        novo_pmp = ((sal_e * pmp_antigo) + (qe * preco_compra)) / total_novas_unidades
                    else:
                        novo_pmp = preco_compra
                        
                    with get_conn() as conn:
                        conn.execute("UPDATE produtos SET saldo_atual = saldo_atual + ?, valor_unitario = ? WHERE id = ?", (qe, novo_pmp, id_pe))
                        data = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
                        obs_completa = f"{obs_e} | Pago: R$ {preco_compra:.2f}/un" if obs_e.strip() else f"Pago: R$ {preco_compra:.2f}/un"
                        conn.execute("INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao) VALUES (?, ?, 'Entrada', ?, ?, ?)", (id_pe, data, qe, total_novas_unidades, obs_completa))
                    
                    disparar_sincronizacao()
                    st.toast(f"📥 Entrada registrada! Novo preço médio de '{sel_e}': R$ {novo_pmp:.2f}", icon="✅")
                    st.success(f"Entrada Confirmada: {sel_e} (+{qe})")
                    st.rerun()

        with col_s:
            with st.container(border=True):
                st.subheader("📤 Registrar Saída")
                sel = st.selectbox("Produto ", list(ops.keys()), key="s_p")
                id_p = ops[sel]
                max_s = int(df.loc[df["id"]==id_p, "saldo_atual"].values[0])
                
                c1, c2 = st.columns([1, 2])
                with c1: q = st.number_input("Quantidade", min_value=1, key="s_q")
                with c2: obs_s = st.text_input("Observação/Destino", key="s_obs")
                
                if q > max_s:
                    st.error(f"❌ Estoque Insuficiente! Saldo atual na prateleira é de apenas {max_s} un.")
                    bloquear_saida = True
                else:
                    bloquear_saida = False
                    
                if st.button("Confirmar Saída", type="primary", disabled=bloquear_saida):
                    with get_conn() as conn:
                        conn.execute("UPDATE produtos SET saldo_atual = saldo_atual - ? WHERE id = ?", (q, id_p))
                        data = datetime.now(ZoneInfo("America/Fortaleza")).strftime("%d/%m/%Y %H:%M")
                        conn.execute("INSERT INTO movimentacoes (id_produto, data_hora, tipo, quantidade, saldo_resultante, observacao) VALUES (?, ?, 'Saída', ?, ?, ?)", (id_p, data, -q, max_s - q, obs_s))
                    disparar_sincronizacao()
                    st.toast(f"📤 Baixa de {q} un. de '{sel}' realizada com sucesso!", icon="🚀")
                    st.success(f"Saída Confirmada: {sel} (-{q})")
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
                st.toast(f"📋 Auditoria de '{sel_c}' gravada e espelhada com sucesso!", icon="💾")
                st.rerun()

        # HISTÓRICO DE DIVERGÊNCIAS COM FILTRO POR INSUMO
        st.divider()
        st.subheader("📉 Relatório de Ajustes e Perdas do Inventário")
        
        prod_lista = ["Todos os Insumos"] + list(df["nome"].unique())
        prod_aud_sel = st.selectbox("Filtrar auditorias por item específico:", prod_lista)
        
        query_hist = """
            SELECT m.data_hora as 'Data/Hora', p.nome as 'Produto', 
                   (m.saldo_resultante - m.quantidade) as 'Saldo Anterior',
                   m.saldo_resultante as 'Contagem Física',
                   m.quantidade as 'Divergência'
            FROM movimentacoes m 
            JOIN produtos p ON p.id = m.id_produto
            WHERE m.tipo = 'Contagem'
        """
        if prod_aud_sel != "Todos os Insumos":
            query_hist += f" AND p.nome = '{prod_aud_sel}'"
            
        query_hist += " ORDER BY m.id DESC LIMIT 15"
        
        with get_conn() as conn:
            hist_inv = pd.read_sql(query_hist, conn)
            
        if not hist_inv.empty:
            def cor_divergencia(val):
                if val < 0: return 'color: #ef4444; font-weight: bold;'
                if val > 0: return 'color: #10b859; font-weight: bold;'
                return 'color: #94a3b8;'
            st.dataframe(hist_inv.style.map(cor_divergencia, subset=['Divergência']), hide_index=True, width='stretch')
        else:
            st.info("Nenhum histórico encontrado para o filtro selecionado.")

# IA ANALISTA
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

# HISTÓRICO
with aba_historico:
    st.subheader("📜 Histórico de Movimentações")
    mv = listar_movimentacoes()
    df_p = listar_produtos()
    
    if not mv.empty:
        st.markdown("##### 📈 Gráfico de Auditoria e Evolução de Preços")
        if not df_p.empty:
            item_analise = st.selectbox("Selecione o Insumo para ver a Curva de Custos:", list(df_p["nome"].unique()))
            entradas_item = mv[(mv["produto"] == item_analise) & (mv["tipo"] == "Entrada")].copy()
            
            if not entradas_item.empty:
                def extrair_preco(obs):
                    try:
                        if "Pago: R$" in str(obs):
                            return float(str(obs).split("Pago: R$ ")[1].split("/un")[0])
                    except:
                        pass
                    return None
                
                entradas_item["Preço de Compra (R$)"] = entradas_item["observacao"].apply(extrair_preco)
                entradas_item = entradas_item.dropna(subset=["Preço de Compra (R$)"])
                
                if not entradas_item.empty:
                    entradas_item = entradas_item.iloc[::-1]
                    st.line_chart(data=entradas_item, x="data_hora", y="Preço de Compra (R$)", width='stretch')
                else:
                    st.info("Este item possui entradas registradas, mas nenhuma com o novo sistema de preços médios ponderados.")
            else:
                st.info("Ainda não existem registros de 'Entrada' para este produto para gerar a linha do tempo.")
        
        st.divider()
        st.markdown("##### 📋 Histórico Geral das Movimentações")
        
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

# GESTÃO DE PRODUTOS
with aba_gestao:
    a1, a2, a3 = st.tabs(["➕ Novo", "✏️ Editar", "🗑️ Excluir"])
    
    with a1:
        with st.form("new_p"):
            n = st.text_input("Nome do Insumo")
            c = st.selectbox("Setor", ["Limpeza", "Copa", "EPI", "Escritório", "Geral"])
            m = st.number_input("Mínimo", value=10)
            l = st.number_input("Lead Time (Dias)", value=3)
            v = st.number_input("Valor Inicial Un. (R$)", value=0.0)
            if st.form_submit_button("Cadastrar"):
                if n.strip():
                    cadastrar_produto(n.strip(), m, v, c, l)
                    disparar_sincronizacao()
                    st.toast(f"➕ Produto '{n.strip()}' cadastrado com sucesso!", icon="✨")
                    st.success(f"Sucesso: Novo insumo cadastrado na base.")
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
                ev = st.number_input("Preço Médio Atual (R$)", value=float(p_at["valor_unitario"]))
                if st.form_submit_button("Atualizar"):
                    editar_produto(id_e, en, em, ev, ec, el)
                    disparar_sincronizacao()
                    st.toast(f"✏️ Configurações de '{en}' atualizadas com sucesso!", icon="⚙️")
                    st.success(f"Sucesso: Dados atualizados.")
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
                try:
                    deletar_produto(id_d)
                    disparar_sincronizacao()
                    st.toast(f"🗑️ Item '{s_d}' foi completamente deletado do cadastro.", icon="🗑️")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro ao deletar produto: {e}")
        else:
            st.info("Nenhum produto cadastrado para exclusão.")