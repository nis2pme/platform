import{O as y,p as A,q as n,e as w,y as I,J as i,a as d,j as l,c as S}from"./vue-vendor-gHMVoUAI.js";import{_ as D,a as N}from"./index-DukJ43Uv.js";const E=["innerHTML"],M={__name:"LegalDialogs",setup(T,{expose:p}){const{locale:c}=y(),s=N(),o=d(!1),t=d("privacidade");function u(e){t.value=e,o.value=!0}p({abrir:u});const g=i(()=>s.deploymentMode!=="saas"?"onprem":s.isTrial?"saas-trial":"saas"),h=i(()=>c.value==="en"?"en":"pt-PT"),m={privacidade:"auth.modal_privacidade_titulo",termos:"auth.modal_termos_titulo"},f={privacidade:{onprem:{"pt-PT":`
        <p>Esta instalação do <strong>NIS2PME</strong> é executada e alojada pela própria organização que a utiliza. O <strong>responsável pelo tratamento</strong> dos dados pessoais aqui processados é essa organização — <strong>não a NIS2PME</strong>, que se limita a fornecer o software (licença AGPL-3.0) e não tem qualquer acesso aos dados desta instalação.</p>
        <h3>Dados tratados nesta instalação</h3>
        <ul>
          <li><strong>Identificação:</strong> nome e endereço de email;</li>
          <li><strong>Autenticação:</strong> palavra-passe (hash Argon2id, irreversível) e segredo TOTP (cifrado em repouso);</li>
          <li><strong>Atividade:</strong> registos de auditoria das ações (endereço IP, data/hora, tipo de ação).</li>
        </ul>
        <h3>Onde ficam os dados</h3>
        <p>Por defeito, todos os dados permanecem na infraestrutura controlada pela sua organização. As únicas ligações para o exterior são a <strong>verificação de atualizações</strong> (opcional e desativável nas Definições) e, caso ative a funcionalidade premium abaixo, a análise por IA.</p>
        <h3>Análise por Inteligência Artificial (funcionalidade premium opcional)</h3>
        <p>A análise por IA é uma funcionalidade <strong>premium e opcional</strong>, desligada por defeito. Quando a sua organização a ativa, o conteúdo das evidências selecionadas é transmitido, através do componente premium, para o <strong>servidor da NIS2PME</strong> que executa a análise — atuando a NIS2PME como <strong>subcontratante</strong> para esse efeito. Sem esta funcionalidade, nenhum conteúdo de evidências sai da instalação.</p>
        <h3>Finalidade e base legal</h3>
        <ul>
          <li><strong>Funcionamento interno</strong> — execução de contrato/relação [Art. 6(1)(b) RGPD];</li>
          <li><strong>Segurança e auditoria de conformidade</strong> — obrigação legal [Art. 6(1)(c) RGPD], no âmbito do Regime Jurídico da Cibersegurança (transposição da Diretiva NIS2);</li>
          <li><strong>Interesses legítimos</strong> — proteção contra acesso não autorizado e fraude [Art. 6(1)(f) RGPD].</li>
        </ul>
        <h3>Retenção</h3>
        <p>Os períodos de retenção são definidos pela sua organização. Recomenda-se manter os registos de auditoria por um mínimo de <strong>12 meses</strong>, em linha com as exigências da NIS2.</p>
        <h3>Direitos do titular</h3>
        <p>Os direitos de acesso, retificação, apagamento, portabilidade e oposição são exercidos junto da <strong>sua organização</strong>, enquanto responsável pelo tratamento. A NIS2PME não pode aceder nem atuar sobre estes dados.</p>
        <h3>Violações de dados pessoais</h3>
        <p>A notificação à CNPD no prazo de 72 horas (Art. 33 RGPD) e aos titulares afetados (Art. 34 RGPD) é da responsabilidade da sua organização, enquanto responsável pelo tratamento.</p>
        <h3>Autoridade de controlo</h3>
        <p>Pode apresentar reclamação à Comissão Nacional de Proteção de Dados (CNPD): <a href="https://www.cnpd.pt" target="_blank" rel="noopener">www.cnpd.pt</a>.</p>
        <h3>Contacto sobre o software</h3>
        <p>Questões sobre o software (não sobre os seus dados pessoais): <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
      `,en:`
        <p>This <strong>NIS2PME</strong> installation is run and hosted by the organisation using it. The <strong>data controller</strong> for the personal data processed here is that organisation — <strong>not NIS2PME</strong>, which only provides the software (AGPL-3.0 licence) and has no access to the data in this installation.</p>
        <h3>Data processed in this installation</h3>
        <ul>
          <li><strong>Identification:</strong> name and email address;</li>
          <li><strong>Authentication:</strong> password (Argon2id hash, irreversible) and TOTP secret (encrypted at rest);</li>
          <li><strong>Activity:</strong> audit logs of actions (IP address, date/time, action type).</li>
        </ul>
        <h3>Where the data lives</h3>
        <p>By default, all data stays on infrastructure controlled by your organisation. The only outbound connections are the <strong>update check</strong> (optional, can be disabled in Settings) and, if you enable the premium feature below, the AI analysis.</p>
        <h3>Artificial Intelligence analysis (optional premium feature)</h3>
        <p>AI analysis is an <strong>optional premium feature</strong>, off by default. When your organisation enables it, the content of the selected evidence is transmitted, via the premium component, to the <strong>NIS2PME server</strong> that performs the analysis — with NIS2PME acting as a <strong>processor</strong> for that purpose. Without this feature, no evidence content leaves the installation.</p>
        <h3>Purpose and legal basis</h3>
        <ul>
          <li><strong>Internal operation</strong> — performance of a contract/relationship [Art. 6(1)(b) GDPR];</li>
          <li><strong>Security and compliance auditing</strong> — legal obligation [Art. 6(1)(c) GDPR], under the Portuguese Cybersecurity Legal Framework (NIS2 Directive transposition);</li>
          <li><strong>Legitimate interests</strong> — protection against unauthorised access and fraud [Art. 6(1)(f) GDPR].</li>
        </ul>
        <h3>Retention</h3>
        <p>Retention periods are set by your organisation. Keeping audit logs for at least <strong>12 months</strong> is recommended, in line with NIS2 requirements.</p>
        <h3>Data subject rights</h3>
        <p>Access, rectification, erasure, portability and objection rights are exercised with <strong>your organisation</strong>, as the data controller. NIS2PME cannot access or act on this data.</p>
        <h3>Personal data breaches</h3>
        <p>Notifying the CNPD within 72 hours (Art. 33 GDPR) and affected data subjects (Art. 34 GDPR) is the responsibility of your organisation, as the data controller.</p>
        <h3>Supervisory authority</h3>
        <p>You may lodge a complaint with the Portuguese Data Protection Authority (CNPD): <a href="https://www.cnpd.pt" target="_blank" rel="noopener">www.cnpd.pt</a>.</p>
        <h3>Software contact</h3>
        <p>Questions about the software (not about your personal data): <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
      `},saas:{"pt-PT":`
        <p>A <strong>NIS2PME</strong> (doravante "Plataforma") é responsável pelo tratamento dos seus dados pessoais, ao abrigo do Regulamento Geral sobre a Proteção de Dados (UE) 2016/679 (RGPD).</p>
        <h3>Dados recolhidos</h3>
        <ul>
          <li><strong>Identificação:</strong> nome e endereço de email;</li>
          <li><strong>Autenticação:</strong> palavra-passe (hash Argon2id, irreversível) e segredo TOTP (cifrado em repouso);</li>
          <li><strong>Atividade:</strong> registos de auditoria das ações (endereço IP, data/hora, tipo de ação).</li>
        </ul>
        <h3>Finalidade e base legal</h3>
        <ul>
          <li><strong>Prestação do serviço</strong> — execução do contrato [Art. 6(1)(b) RGPD];</li>
          <li><strong>Segurança e auditoria de conformidade</strong> — obrigação legal [Art. 6(1)(c) RGPD];</li>
          <li><strong>Interesses legítimos</strong> — proteção contra acesso não autorizado e fraude [Art. 6(1)(f) RGPD].</li>
        </ul>
        <h3>Período de retenção</h3>
        <p>Os dados de conta são retidos enquanto a conta estiver ativa. Os registos de auditoria são mantidos por um mínimo de <strong>12 meses</strong>. Após o encerramento da conta, os dados pessoais são anonimizados.</p>
        <h3>Análise por Inteligência Artificial (premium)</h3>
        <p>Quando utiliza a análise por IA, o conteúdo das evidências é transmitido, através do componente premium, e processado na <strong>infraestrutura da NIS2PME</strong>. O tratamento é efetuado pela NIS2PME e, quando aplicável, pelos seus subcontratantes de IA, ao abrigo de garantias contratuais adequadas e, no caso de transferências internacionais, dos mecanismos do Capítulo V do RGPD. As evidências são tratadas apenas para produzir a análise pedida, não sendo usadas para treinar modelos.</p>
        <h3>Direitos do titular</h3>
        <p>Tem direito a aceder, retificar, apagar (com as limitações do Art. 17(3)(b) RGPD para registos de auditoria), portar e opor-se ao tratamento. Para exercer os seus direitos: <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
        <h3>Subcontratantes e alojamento</h3>
        <p>Os dados são alojados em infraestrutura gerida pela NIS2PME, com transporte protegido por TLS 1.2+. Não partilhamos dados com terceiros para fins comerciais ou de marketing.</p>
        <h3>Violações de dados pessoais</h3>
        <p>Em caso de violação com risco para os titulares, a NIS2PME notifica a CNPD no prazo de 72 horas (Art. 33 RGPD) e os titulares afetados sem demora injustificada (Art. 34 RGPD).</p>
        <h3>Autoridade de controlo</h3>
        <p>Pode apresentar reclamação à CNPD: <a href="https://www.cnpd.pt" target="_blank" rel="noopener">www.cnpd.pt</a>.</p>
      `,en:`
        <p><strong>NIS2PME</strong> (the "Platform") is the controller of your personal data under the General Data Protection Regulation (EU) 2016/679 (GDPR).</p>
        <h3>Data collected</h3>
        <ul>
          <li><strong>Identification:</strong> name and email address;</li>
          <li><strong>Authentication:</strong> password (Argon2id hash, irreversible) and TOTP secret (encrypted at rest);</li>
          <li><strong>Activity:</strong> audit logs of actions (IP address, date/time, action type).</li>
        </ul>
        <h3>Purpose and legal basis</h3>
        <ul>
          <li><strong>Service provision</strong> — performance of the contract [Art. 6(1)(b) GDPR];</li>
          <li><strong>Security and compliance auditing</strong> — legal obligation [Art. 6(1)(c) GDPR];</li>
          <li><strong>Legitimate interests</strong> — protection against unauthorised access and fraud [Art. 6(1)(f) GDPR].</li>
        </ul>
        <h3>Retention period</h3>
        <p>Account data is retained while the account is active. Audit logs are kept for at least <strong>12 months</strong>. After account closure, personal data is anonymised.</p>
        <h3>Artificial Intelligence analysis (premium)</h3>
        <p>When you use AI analysis, the content of the evidence is transmitted, via the premium component, and processed on <strong>NIS2PME infrastructure</strong>. Processing is carried out by NIS2PME and, where applicable, by its AI processors, under appropriate contractual safeguards and, for international transfers, the mechanisms of Chapter V GDPR. Evidence is processed solely to produce the requested analysis and is not used to train models.</p>
        <h3>Data subject rights</h3>
        <p>You have the right to access, rectify, erase (subject to Art. 17(3)(b) GDPR limits for audit logs), port and object to processing. To exercise your rights: <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
        <h3>Processors and hosting</h3>
        <p>Data is hosted on infrastructure managed by NIS2PME, with transport protected by TLS 1.2+. We do not share data with third parties for commercial or marketing purposes.</p>
        <h3>Personal data breaches</h3>
        <p>In the event of a breach posing a risk to data subjects, NIS2PME notifies the CNPD within 72 hours (Art. 33 GDPR) and affected data subjects without undue delay (Art. 34 GDPR).</p>
        <h3>Supervisory authority</h3>
        <p>You may lodge a complaint with the CNPD: <a href="https://www.cnpd.pt" target="_blank" rel="noopener">www.cnpd.pt</a>.</p>
      `},"saas-trial":{"pt-PT":`
        <div class="aviso-trial"><strong>Conta de avaliação (trial).</strong> Este é um ambiente de teste gratuito. Os dados são eliminados automaticamente ao fim de <strong>14 dias</strong>. <strong>Não introduza dados pessoais reais nem informação sensível</strong> nas evidências ou nos campos da plataforma.</div>
        <p>A <strong>NIS2PME</strong> é responsável pelo tratamento dos dados estritamente necessários para disponibilizar este ambiente de avaliação, ao abrigo do RGPD.</p>
        <h3>Dados recolhidos</h3>
        <ul>
          <li><strong>Identificação:</strong> nome e endereço de email;</li>
          <li><strong>Autenticação:</strong> palavra-passe (hash Argon2id, irreversível) e segredo TOTP (cifrado em repouso);</li>
          <li><strong>Atividade:</strong> registos de auditoria das ações (endereço IP, data/hora, tipo de ação);</li>
          <li><strong>Conteúdos introduzidos por si:</strong> evidências e dados que carregar — que <strong>não devem conter</strong> dados pessoais nem informação sensível.</li>
        </ul>
        <h3>Finalidade e base legal</h3>
        <ul>
          <li><strong>Disponibilização do ambiente de avaliação</strong> — diligências pré-contratuais a seu pedido [Art. 6(1)(b) RGPD];</li>
          <li><strong>Segurança e prevenção de abuso</strong> — interesses legítimos [Art. 6(1)(f) RGPD].</li>
        </ul>
        <h3>Análise por Inteligência Artificial</h3>
        <p>Se utilizar a análise por IA, o conteúdo das evidências é transmitido, através do componente premium, e processado por <strong>fornecedores de IA</strong> na infraestrutura da NIS2PME. <strong>Por se tratar de uma conta de avaliação, não submeta dados pessoais nem confidenciais para análise.</strong></p>
        <h3>Período de retenção</h3>
        <p>Todos os dados desta conta de avaliação são <strong>eliminados automaticamente ao fim de 14 dias</strong>, sem possibilidade de recuperação. Pode também solicitar a eliminação antecipada.</p>
        <h3>Direitos do titular</h3>
        <p>Pode aceder, retificar, apagar, portar e opor-se ao tratamento. Para exercer os seus direitos: <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
        <h3>Alojamento</h3>
        <p>Os dados são alojados em infraestrutura gerida pela NIS2PME, com transporte protegido por TLS 1.2+. Não partilhamos dados com terceiros para fins comerciais ou de marketing.</p>
        <h3>Autoridade de controlo</h3>
        <p>Pode apresentar reclamação à CNPD: <a href="https://www.cnpd.pt" target="_blank" rel="noopener">www.cnpd.pt</a>.</p>
      `,en:`
        <div class="aviso-trial"><strong>Evaluation account (trial).</strong> This is a free test environment. Data is automatically deleted after <strong>14 days</strong>. <strong>Do not enter real personal data or sensitive information</strong> in evidence or platform fields.</div>
        <p><strong>NIS2PME</strong> is the controller of the data strictly necessary to provide this evaluation environment, under the GDPR.</p>
        <h3>Data collected</h3>
        <ul>
          <li><strong>Identification:</strong> name and email address;</li>
          <li><strong>Authentication:</strong> password (Argon2id hash, irreversible) and TOTP secret (encrypted at rest);</li>
          <li><strong>Activity:</strong> audit logs of actions (IP address, date/time, action type);</li>
          <li><strong>Content you enter:</strong> evidence and data you upload — which <strong>must not contain</strong> personal data or sensitive information.</li>
        </ul>
        <h3>Purpose and legal basis</h3>
        <ul>
          <li><strong>Provision of the evaluation environment</strong> — pre-contractual steps at your request [Art. 6(1)(b) GDPR];</li>
          <li><strong>Security and abuse prevention</strong> — legitimate interests [Art. 6(1)(f) GDPR].</li>
        </ul>
        <h3>Artificial Intelligence analysis</h3>
        <p>If you use AI analysis, the content of the evidence is transmitted, via the premium component, and processed by <strong>AI providers</strong> on NIS2PME infrastructure. <strong>As this is an evaluation account, do not submit personal or confidential data for analysis.</strong></p>
        <h3>Retention period</h3>
        <p>All data in this evaluation account is <strong>automatically deleted after 14 days</strong>, with no possibility of recovery. You may also request earlier deletion.</p>
        <h3>Data subject rights</h3>
        <p>You may access, rectify, erase, port and object to processing. To exercise your rights: <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
        <h3>Hosting</h3>
        <p>Data is hosted on infrastructure managed by NIS2PME, with transport protected by TLS 1.2+. We do not share data with third parties for commercial or marketing purposes.</p>
        <h3>Supervisory authority</h3>
        <p>You may lodge a complaint with the CNPD: <a href="https://www.cnpd.pt" target="_blank" rel="noopener">www.cnpd.pt</a>.</p>
      `}},termos:{onprem:{"pt-PT":`
        <p>O <strong>NIS2PME</strong> é software de apoio à conformidade, auto-alojado e distribuído sob a licença <strong>AGPL-3.0</strong>. Ao utilizar esta instalação, aceita os presentes termos.</p>
        <h3>Descrição</h3>
        <p>Ferramenta de apoio à conformidade com o Regime Jurídico da Cibersegurança (transposição da Diretiva NIS2): diagnóstico, gestão de controlos, gestão de evidências e relatórios de conformidade.</p>
        <h3>Fornecido "tal como está"</h3>
        <p>O software é fornecido <em>as-is</em>, sem garantias de qualquer tipo e sem SLA. A instalação, operação, disponibilidade, cópias de segurança e manutenção são da inteira responsabilidade da organização que o aloja.</p>
        <h3>Obrigações de quem opera/utiliza</h3>
        <ul>
          <li>Manter a confidencialidade das credenciais de acesso;</li>
          <li>Ativar a autenticação em dois fatores (2FA) quando exigida pela função;</li>
          <li>Utilizar a plataforma apenas para fins lícitos e no âmbito da conformidade da organização;</li>
          <li>Manter o software atualizado e a infraestrutura segura;</li>
          <li>Não partilhar acessos com terceiros não autorizados.</li>
        </ul>
        <h3>Limitação de responsabilidade</h3>
        <p>A NIS2PME não se responsabiliza por decisões de conformidade tomadas com base nos resultados da plataforma, nem pela operação, disponibilidade ou integridade desta instalação, que dependem exclusivamente de quem a aloja.</p>
        <h3>Propriedade intelectual</h3>
        <p>O código-fonte é distribuído sob <strong>AGPL-3.0</strong>. A marca e a documentação são propriedade da NIS2PME.</p>
        <h3>Lei aplicável</h3>
        <p>Os presentes termos são regidos pela lei portuguesa.</p>
      `,en:`
        <p><strong>NIS2PME</strong> is self-hosted compliance-support software distributed under the <strong>AGPL-3.0</strong> licence. By using this installation, you accept these terms.</p>
        <h3>Description</h3>
        <p>A tool to support compliance with the Portuguese Cybersecurity Legal Framework (NIS2 Directive transposition): assessment, control management, evidence management and compliance reporting.</p>
        <h3>Provided "as is"</h3>
        <p>The software is provided <em>as-is</em>, without warranties of any kind and without an SLA. Installation, operation, availability, backups and maintenance are entirely the responsibility of the organisation hosting it.</p>
        <h3>Operator/user obligations</h3>
        <ul>
          <li>Keep access credentials confidential;</li>
          <li>Enable two-factor authentication (2FA) when required by your role;</li>
          <li>Use the platform only for lawful purposes and within the organisation's compliance scope;</li>
          <li>Keep the software updated and the infrastructure secure;</li>
          <li>Do not share access with unauthorised third parties.</li>
        </ul>
        <h3>Limitation of liability</h3>
        <p>NIS2PME is not liable for compliance decisions made based on the platform's outputs, nor for the operation, availability or integrity of this installation, which depend solely on whoever hosts it.</p>
        <h3>Intellectual property</h3>
        <p>The source code is distributed under <strong>AGPL-3.0</strong>. The brand and documentation are the property of NIS2PME.</p>
        <h3>Governing law</h3>
        <p>These terms are governed by Portuguese law.</p>
      `},saas:{"pt-PT":`
        <p>Ao aceder à plataforma <strong>NIS2PME</strong>, aceita os presentes Termos de Serviço.</p>
        <h3>Descrição do serviço</h3>
        <p>A NIS2PME é uma plataforma de apoio à conformidade com o Regime Jurídico da Cibersegurança (transposição da Diretiva NIS2): diagnóstico, gestão de controlos, upload de evidências e relatórios de conformidade.</p>
        <h3>Obrigações do utilizador</h3>
        <ul>
          <li>Manter a confidencialidade das credenciais de acesso;</li>
          <li>Ativar a autenticação em dois fatores (2FA) quando exigida pela função;</li>
          <li>Não partilhar o acesso com terceiros não autorizados;</li>
          <li>Utilizar a plataforma exclusivamente para fins lícitos;</li>
          <li>Notificar a NIS2PME em caso de suspeita de acesso não autorizado.</li>
        </ul>
        <h3>Disponibilidade</h3>
        <p>O serviço é disponibilizado <em>as-is</em>. A NIS2PME envidará os seus melhores esforços para assegurar a disponibilidade e a integridade dos dados.</p>
        <h3>Limitação de responsabilidade</h3>
        <p>A NIS2PME não se responsabiliza por decisões de conformidade tomadas com base exclusiva nos resultados da plataforma. O utilizador é responsável pela verificação final da conformidade.</p>
        <h3>Propriedade intelectual</h3>
        <p>O código-fonte é distribuído sob a licença <strong>AGPL-3.0</strong>. Os conteúdos, marca e documentação são propriedade da NIS2PME.</p>
        <h3>Lei aplicável</h3>
        <p>Os presentes Termos são regidos pela lei portuguesa.</p>
      `,en:`
        <p>By accessing the <strong>NIS2PME</strong> platform, you accept these Terms of Service.</p>
        <h3>Service description</h3>
        <p>NIS2PME is a platform to support compliance with the Portuguese Cybersecurity Legal Framework (NIS2 Directive transposition): assessment, control management, evidence upload and compliance reporting.</p>
        <h3>User obligations</h3>
        <ul>
          <li>Keep access credentials confidential;</li>
          <li>Enable two-factor authentication (2FA) when required by your role;</li>
          <li>Do not share access with unauthorised third parties;</li>
          <li>Use the platform solely for lawful purposes;</li>
          <li>Notify NIS2PME if you suspect unauthorised access.</li>
        </ul>
        <h3>Availability</h3>
        <p>The service is provided <em>as-is</em>. NIS2PME will use its best efforts to ensure availability and data integrity.</p>
        <h3>Limitation of liability</h3>
        <p>NIS2PME is not liable for compliance decisions made solely on the basis of the platform's outputs. The user is responsible for the final verification of compliance.</p>
        <h3>Intellectual property</h3>
        <p>The source code is distributed under the <strong>AGPL-3.0</strong> licence. Content, brand and documentation are the property of NIS2PME.</p>
        <h3>Governing law</h3>
        <p>These Terms are governed by Portuguese law.</p>
      `},"saas-trial":{"pt-PT":`
        <div class="aviso-trial"><strong>Conta de avaliação (trial).</strong> Ambiente de teste gratuito, fornecido "tal como está", sem garantias nem SLA. Os dados são eliminados automaticamente ao fim de <strong>14 dias</strong>. Não utilize dados reais, pessoais ou confidenciais.</div>
        <p>Ao aceder ao ambiente de avaliação <strong>NIS2PME</strong>, aceita os presentes Termos.</p>
        <h3>Descrição</h3>
        <p>Ambiente gratuito de avaliação da plataforma de apoio à conformidade com o Regime Jurídico da Cibersegurança (transposição da Diretiva NIS2). Destina-se exclusivamente a teste e demonstração.</p>
        <h3>Natureza de avaliação</h3>
        <ul>
          <li>Conta gratuita e temporária, com <strong>limites de utilização</strong> (incluindo quota de análises por IA);</li>
          <li>Os dados são <strong>descartáveis</strong> e eliminados ao fim de 14 dias, sem possibilidade de recuperação;</li>
          <li><strong>Um trial por endereço de email</strong>;</li>
          <li>A NIS2PME pode suspender ou encerrar contas de avaliação a qualquer momento.</li>
        </ul>
        <h3>Utilização aceitável</h3>
        <ul>
          <li><strong>Não introduzir dados pessoais reais nem informação confidencial/sensível;</strong></li>
          <li>Utilizar a plataforma apenas para fins lícitos e de avaliação;</li>
          <li>Não tentar contornar os limites, a segurança ou a unicidade do trial.</li>
        </ul>
        <h3>Sem garantias</h3>
        <p>O ambiente de avaliação é fornecido <em>as-is</em>, sem garantias de disponibilidade, desempenho ou integridade dos dados, e sem SLA.</p>
        <h3>Limitação de responsabilidade</h3>
        <p>A NIS2PME não se responsabiliza por quaisquer dados introduzidos no ambiente de avaliação nem por decisões tomadas com base nos seus resultados.</p>
        <h3>Propriedade intelectual</h3>
        <p>O código-fonte é distribuído sob a licença <strong>AGPL-3.0</strong>. Os conteúdos, marca e documentação são propriedade da NIS2PME.</p>
        <h3>Lei aplicável</h3>
        <p>Os presentes Termos são regidos pela lei portuguesa.</p>
      `,en:`
        <div class="aviso-trial"><strong>Evaluation account (trial).</strong> Free test environment, provided "as is", without warranties or SLA. Data is automatically deleted after <strong>14 days</strong>. Do not use real, personal or confidential data.</div>
        <p>By accessing the <strong>NIS2PME</strong> evaluation environment, you accept these Terms.</p>
        <h3>Description</h3>
        <p>A free environment to evaluate the platform supporting compliance with the Portuguese Cybersecurity Legal Framework (NIS2 Directive transposition). It is intended solely for testing and demonstration.</p>
        <h3>Evaluation nature</h3>
        <ul>
          <li>Free, temporary account with <strong>usage limits</strong> (including an AI analysis quota);</li>
          <li>Data is <strong>disposable</strong> and deleted after 14 days, with no possibility of recovery;</li>
          <li><strong>One trial per email address</strong>;</li>
          <li>NIS2PME may suspend or close evaluation accounts at any time.</li>
        </ul>
        <h3>Acceptable use</h3>
        <ul>
          <li><strong>Do not enter real personal data or confidential/sensitive information;</strong></li>
          <li>Use the platform only for lawful and evaluation purposes;</li>
          <li>Do not attempt to bypass the limits, security or one-trial-per-email rule.</li>
        </ul>
        <h3>No warranties</h3>
        <p>The evaluation environment is provided <em>as-is</em>, without warranties of availability, performance or data integrity, and without an SLA.</p>
        <h3>Limitation of liability</h3>
        <p>NIS2PME is not liable for any data entered into the evaluation environment nor for decisions made based on its outputs.</p>
        <h3>Intellectual property</h3>
        <p>The source code is distributed under the <strong>AGPL-3.0</strong> licence. Content, brand and documentation are the property of NIS2PME.</p>
        <h3>Governing law</h3>
        <p>These Terms are governed by Portuguese law.</p>
      `}}},v=i(()=>{var e,a;return((a=(e=f[t.value])==null?void 0:e[g.value])==null?void 0:a[h.value])??""});return(e,a)=>{const b=l("Button"),P=l("Dialog");return S(),A(P,{visible:o.value,"onUpdate:visible":a[1]||(a[1]=r=>o.value=r),header:e.$t(m[t.value]),modal:!0,style:{width:"680px",maxWidth:"96vw"},"dismissable-mask":!0},{footer:n(()=>[I(b,{label:e.$t("geral.fechar"),severity:"secondary",onClick:a[0]||(a[0]=r=>o.value=!1)},null,8,["label"])]),default:n(()=>[w("div",{class:"legal-dialog",innerHTML:v.value},null,8,E)]),_:1},8,["visible","header"])}}},R=D(M,[["__scopeId","data-v-840c309d"]]);export{R as L};
