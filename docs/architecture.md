# Arquitetura e plano

MyClipFactory processa gravações autorizadas de gameplay para revisão editorial. A aplicação atende um operador por instalação e usa uma interface HTTP de mesma origem.

1. Ingestão. O servidor recebe o MP4 em fluxo, persiste o original e lê metadados com FFprobe. O operador inicia um job durável. O worker valida a decodificação e extrai áudio e amostras visuais.
2. Análise. O transcritor devolve segmentos e palavras com timestamps absolutos, probabilidades e marcações de revisão. A análise junta fala, intensidade e movimento em candidatos com janela, pontuação, evidências e plano de enquadramento. O resultado é persistido antes da renderização.
3. Renderização. O editor recebe a janela, o plano visual e a transcrição corrigida. FFmpeg gera MP4 9:16 com H.264/AAC e legendas ASS. A revisão registra versões de edição e invalida as aprovações pendentes da versão anterior.
4. Publicação. A fila guarda data de execução e aprovação da versão por conta. Cada adaptador recebe os metadados aprovados e registra o identificador remoto antes de consultar o resultado. Erros ambíguos exigem reconciliação antes de uma nova tentativa.
5. Aceitação. Executar testes de validação de mídia, análise, renderização, revisão e contratos HTTP de publicação. Validar inferência com um modelo instalado e publicação real com aplicativos autorizados quando credenciais e permissões estiverem disponíveis.

## Dependências e riscos

FFmpeg, FFprobe e o modelo local permitem processamento sem custo por chamada. O primeiro uso do transcritor baixa pesos, salvo quando configurado com um diretório de modelo local. Espaço de armazenamento, CPU/GPU, permissões das plataformas e URL HTTPS acessível pelo Instagram são responsabilidades da instalação. A seleção multimodal local usa sinais observáveis, e a revisão humana continua necessária para julgar contexto e direitos sobre o material. O TikTok restringe ferramentas internas de postagem e exige avaliação do aplicativo para Direct Post público.
