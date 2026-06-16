import{O as y,p as w,q as r,e as A,y as D,J as s,a as n,j as d,c as S}from"./vue-vendor-DpDucEUS.js";import{_ as I,b as N}from"./index-ZkaJxgkP.js";const E=["innerHTML"],G={__name:"LegalDialogs",setup(M,{expose:l}){const{locale:p}=y(),c=N(),a=n(!1),t=n("privacidade");function u(e){t.value=e,a.value=!0}l({abrir:u});const h=s(()=>c.deploymentMode==="saas"?"saas":"onprem"),g=s(()=>p.value==="en"?"en":"pt-PT"),m={privacidade:"auth.modal_privacidade_titulo",termos:"auth.modal_termos_titulo"},f={privacidade:{onprem:{"pt-PT":`
        <p>Esta instalação do <strong>NIS2PME</strong> é executada e alojada pela própria organização que a utiliza. O <strong>responsável pelo tratamento</strong> dos dados pessoais aqui processados é essa organização — <strong>não a NIS2PME</strong>, que se limita a fornecer o software (licença AGPL-3.0) e não tem qualquer acesso aos dados desta instalação.</p>
        <h3>Dados tratados nesta instalação</h3>
        <ul>
          <li><strong>Identificação:</strong> nome e endereço de email;</li>
          <li><strong>Autenticação:</strong> palavra-passe (hash Argon2id, irreversível) e segredo TOTP (cifrado em repouso);</li>
          <li><strong>Atividade:</strong> registos de auditoria das ações (endereço IP, data/hora, tipo de ação).</li>
        </ul>
        <h3>Onde ficam os dados</h3>
        <p>Todos os dados permanecem na infraestrutura controlada pela sua organização. A única ligação para o exterior é a <strong>verificação de atualizações</strong> (opcional e desativável nas Definições).</p>
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
        <p>All data stays on infrastructure controlled by your organisation. The only outbound connection is the <strong>update check</strong> (optional, can be disabled in Settings)</p>
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
        <h3>Data subject rights</h3>
        <p>You have the right to access, rectify, erase (subject to Art. 17(3)(b) GDPR limits for audit logs), port and object to processing. To exercise your rights: <a href="mailto:contact@nis2pme.pt">contact@nis2pme.pt</a>.</p>
        <h3>Processors and hosting</h3>
        <p>Data is hosted on infrastructure managed by NIS2PME, with transport protected by TLS 1.2+. We do not share data with third parties for commercial or marketing purposes.</p>
        <h3>Personal data breaches</h3>
        <p>In the event of a breach posing a risk to data subjects, NIS2PME notifies the CNPD within 72 hours (Art. 33 GDPR) and affected data subjects without undue delay (Art. 34 GDPR).</p>
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
      `}}},b=s(()=>{var e,o;return((o=(e=f[t.value])==null?void 0:e[h.value])==null?void 0:o[g.value])??""});return(e,o)=>{const P=d("Button"),v=d("Dialog");return S(),w(v,{visible:a.value,"onUpdate:visible":o[1]||(o[1]=i=>a.value=i),header:e.$t(m[t.value]),modal:!0,style:{width:"680px",maxWidth:"96vw"},"dismissable-mask":!0},{footer:r(()=>[D(P,{label:e.$t("geral.fechar"),severity:"secondary",onClick:o[0]||(o[0]=i=>a.value=!1)},null,8,["label"])]),default:r(()=>[A("div",{class:"legal-dialog",innerHTML:b.value},null,8,E)]),_:1},8,["visible","header"])}}},L=I(G,[["__scopeId","data-v-e0cef490"]]);export{L};
