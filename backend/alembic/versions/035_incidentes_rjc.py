"""Incidentes pelo Regime Jurídico da Cibersegurança.

Os marcos deixam de ser os da Diretiva (alerta precoce, notificação, relatório
a um mês) e passam a ser os do regime nacional: notificação inicial,
atualização, fim de impacto significativo, relatório final (30 dias úteis),
relatórios intercalares e, para violações de dados pessoais, a notificação à
CNPD. Por isso:

  - as colunas dos dois primeiros marcos mudam de nome (o que estava registado
    como alerta precoce é a notificação inicial; a notificação das 72 h é a
    atualização);
  - entram as colunas dos factos de que os novos prazos dependem e dos campos
    que as notificações têm de levar;
  - nasce `incidente_notificacoes`, só de acrescentar, com a cópia cifrada de
    cada notificação enviada;
  - a categoria passa a ser um código da taxonomia de incidentes (o mapa das
    categorias antigas está aqui dentro, porque uma migração não pode depender
    de um ficheiro de dados que muda);
  - onde o incidente já era significativo, a data em que o passou a ser fica a
    do conhecimento (a base que os prazos usavam);
  - os avisos de prazos antigos saem da lista: descreviam prazos que deixaram
    de existir, e o tick seguinte cria os novos.

Uma instalação nova já recebe tudo dos modelos: cada passo verifica primeiro se
há alguma coisa a fazer, e correr duas vezes não muda nada.

Revision ID: 035_incidentes_rjc
Revises: 034_totp_ultimo_passo
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "035_incidentes_rjc"
down_revision: Union[str, None] = "034_totp_ultimo_passo"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RENOMEAR = (
    ("alerta_precoce_at", "notificacao_inicial_at"),
    ("notificacao_at", "atualizacao_at"),
)

# (nome, tipo, nulo). Os booleanos obrigatórios entram com um valor por omissão
# no servidor só para preencher as linhas que já existem; os modelos não o têm,
# por isso sai logo a seguir.
_COLUNAS = (
    ("significativo_em", sa.DateTime(), True),
    ("impacto_inicio_em", sa.DateTime(), True),
    ("fim_impacto_em", sa.DateTime(), True),
    ("fim_impacto_notificado_at", sa.DateTime(), True),
    ("resolvido_2h", sa.Boolean(), False),
    ("atualizacao_necessaria", sa.Boolean(), False),
    ("excecao_24h", sa.Text(), True),
    ("intercalar_pedido_em", sa.DateTime(), True),
    ("cnpd_aplicavel", sa.Boolean(), False),
    ("cnpd_notificado_at", sa.DateTime(), True),
    ("representante_nome", sa.Text(), True),
    ("representante_telefone", sa.Text(), True),
    ("representante_email", sa.Text(), True),
    ("utilizadores_afetados", sa.Integer(), True),
    ("utilizadores_total", sa.Integer(), True),
    ("zona_geografica", sa.String(length=255), True),
    ("transfronteirico", sa.Boolean(), True),
    ("paises_afetados", sa.String(length=255), True),
    ("causa", sa.Text(), True),
    ("efeitos", sa.Text(), True),
    ("medidas", sa.Text(), True),
    ("situacao_residual", sa.Text(), True),
    ("tempo_recuperacao", sa.String(length=120), True),
)

# Categorias antigas → tipo da taxonomia (Taxonomia Comum da RNCSIRT v3.3).
_CATEGORIAS = {
    "ransomware": "seguranca_informacao.modificacao_nao_autorizada",
    "phishing": "fraude.phishing",
    "fuga_dados": "seguranca_informacao.exfiltracao",
    "indisponibilidade": "disponibilidade.interrupcao",
    "acesso_indevido": "seguranca_informacao.acesso_nao_autorizado",
    "malware": "codigo_malicioso.sistema_infetado",
    "outro": "outro.sem_tipo",
}

_TABELA = "incidente_notificacoes"
_INDICES = ("id", "incidente_id", "empresa_id", "autor_id")


def _colunas(inspector, tabela: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(tabela)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "incidentes" not in inspector.get_table_names():
        return
    existentes = _colunas(inspector, "incidentes")

    for antigo, novo in _RENOMEAR:
        if antigo in existentes and novo not in existentes:
            op.alter_column("incidentes", antigo, new_column_name=novo)

    for nome, tipo, nulo in _COLUNAS:
        if nome in existentes:
            continue
        if nulo:
            op.add_column("incidentes", sa.Column(nome, tipo, nullable=True))
        else:
            op.add_column(
                "incidentes",
                sa.Column(nome, tipo, nullable=False, server_default=sa.false()),
            )
            if bind.dialect.name != "sqlite":  # o SQLite não altera colunas
                op.alter_column("incidentes", nome, server_default=None)

    if _TABELA not in inspector.get_table_names():
        op.create_table(
            _TABELA,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("incidente_id", sa.Uuid(), nullable=False),
            sa.Column("empresa_id", sa.Uuid(), nullable=False),
            sa.Column("tipo", sa.String(length=40), nullable=False),
            sa.Column("enviada_em", sa.DateTime(), nullable=False),
            sa.Column("canal", sa.String(length=40), nullable=False),
            sa.Column("referencia", sa.String(length=255), nullable=True),
            sa.Column("conteudo", sa.Text(), nullable=True),
            sa.Column("hash_conteudo", sa.String(length=64), nullable=False),
            sa.Column("autor_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["incidente_id"], ["incidentes.id"]),
            sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
            sa.ForeignKeyConstraint(["autor_id"], ["utilizadores.id"]),
        )
        for coluna in _INDICES:
            op.create_index(f"ix_{_TABELA}_{coluna}", _TABELA, [coluna])

    for antiga, nova in _CATEGORIAS.items():
        bind.execute(
            sa.text("UPDATE incidentes SET categoria = :nova WHERE categoria = :antiga"),
            {"nova": nova, "antiga": antiga},
        )
    bind.execute(sa.text(
        "UPDATE incidentes SET significativo_em = conhecido_at "
        "WHERE significativo IS TRUE AND significativo_em IS NULL"
    ))

    # Avisos com a chave antiga (código:incidente:marco), de marcos que já não
    # existem ou cujo prazo mudou. Dão-se por resolvidos, como quando o facto
    # deixa de ser verdade; nada se apaga.
    if "notificacoes" in inspector.get_table_names():
        bind.execute(
            sa.text(
                "UPDATE notificacoes SET lida = :sim, acionavel = :nao, "
                "lida_at = COALESCE(lida_at, CURRENT_TIMESTAMP) "
                "WHERE codigo IN ('incidente.prazo_risco', 'incidente.prazo_atraso') "
                "AND (chave_dedup LIKE :m1 OR chave_dedup LIKE :m2 OR chave_dedup LIKE :m3) "
                "AND (lida = :nao OR acionavel = :sim)"
            ),
            {
                "sim": True, "nao": False,
                "m1": "%:alerta_precoce", "m2": "%:notificacao", "m3": "%:relatorio_final",
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "incidentes" not in inspector.get_table_names():
        return

    if _TABELA in inspector.get_table_names():
        op.drop_table(_TABELA)

    existentes = _colunas(inspector, "incidentes")
    for nome, _tipo, _nulo in _COLUNAS:
        if nome in existentes:
            op.drop_column("incidentes", nome)
    for antigo, novo in _RENOMEAR:
        if novo in existentes and antigo not in existentes:
            op.alter_column("incidentes", novo, new_column_name=antigo)

    # Tipos da taxonomia → categoria antiga; o que não tinha correspondência
    # volta a «outro».
    inverso = {nova: antiga for antiga, nova in _CATEGORIAS.items()}
    for nova, antiga in inverso.items():
        bind.execute(
            sa.text("UPDATE incidentes SET categoria = :antiga WHERE categoria = :nova"),
            {"nova": nova, "antiga": antiga},
        )
    bind.execute(
        sa.text("UPDATE incidentes SET categoria = 'outro' WHERE categoria NOT IN :antigas")
        .bindparams(sa.bindparam("antigas", expanding=True)),
        {"antigas": list(_CATEGORIAS)},
    )
