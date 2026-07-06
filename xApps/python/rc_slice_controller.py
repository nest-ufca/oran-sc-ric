#!/usr/bin/env python3

import time
import csv
import time
import datetime
import argparse
import signal
from lib.xAppBase import xAppBase


UE_TO_SLICE = {
    0: "SST_1_eMBB",
    1: "SST_1_eMBB",
    2: "SST_1_eMBB",
    3: "SST_2_URLLC",
    4: "SST_2_URLLC",
    5: "SST_2_URLLC",
    6: "SST_3_mMTC",
    7: "SST_3_mMTC",
}


SLICE_POLICY = {
    "SST_1_eMBB": {
        "priority": 2,
        "weight": 0.1,
        "target_metric": "throughput",
        "target_throughput_dl": 8000,
        "target_delay_dl": 50,
    },
    "SST_2_URLLC": {
        "priority": 3,
        "weight": 0.1,
        "target_metric": "delay",
        "target_throughput_dl": 3000,
        "target_delay_dl": 20,
    },
    "SST_3_mMTC": {
        "priority": 1,
        "weight": 0.1,
        "target_metric": "basic_connectivity",
        "target_throughput_dl": 1000,
        "target_delay_dl": 100,
    },
}

class MyXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)
        pass

    @staticmethod
    def to_float(value, default=None):
    # Tenta converter qualquer valor recebido para float.
    # Essa função é staticmethod porque não usa nenhum atributo do objeto self.
        try:
         # Se o valor vier vazio, None, ou como texto "None", retorna o valor padrão informado em default.
            if value in ["", None, "None"]:
                return default
            
            # Se o valor vier como string, limpamos espaços extras.
            if isinstance(value, str):
                value = value.strip()

                # Caso o CSV tenha salvo o valor como lista em texto, por exemplo "[4105.0]", removemos os colchetes.
                if value.startswith("[") and value.endswith("]"):
                    value = value[1:-1].strip()

            # Depois da limpeza, tenta converter para float.
            return float(value)
        
        # Se a conversão falhar, retorna o valor padrão.
        # ValueError: texto não numérico, exemplo "abc".
        # TypeError: tipo incompatível.
        except (ValueError, TypeError):
            return default
        
    def read_latest_kpm_by_ue(self, csv_path, max_age_s=10):
        # Dicionário onde vamos guardar apenas a métrica mais recente de cada UE.
        # Exemplo:
        # {
        #   "0": linha_mais_recente_do_ue_0,
        #   "1": linha_mais_recente_do_ue_1,
        # }
        latest_by_ue = {}

        try:
            # Abre o CSV de métricas KPM em modo leitura.
            with open(csv_path, mode="r", newline="") as file:
                # DictReader lê cada linha como um dicionário:
                # row["ue_id"], row["timestamp_unix"], row["DRB.UEThpDl"], etc.
                reader = csv.DictReader(file)
    
                # Percorre todas as linhas do CSV.
                for row in reader:
                    # Pega o UE ID da linha atual.
                    ue_id = row.get("ue_id")
                    
                    # Ignora linhas sem UE ID. Isso pode acontecer em métricas agregadas Style 1.
                    if ue_id in ["", None]:
                        continue
    
                    # Pega o timestamp da linha e converte para float.
                    timestamp = self.to_float(row.get("timestamp_unix"))

                    # Se não conseguiu converter o timestamp, ignora essa linha.
                    if timestamp is None:
                        continue

                    # Ignora métricas antigas.
                    # Exemplo: se max_age_s = 10, só aceita métricas dos últimos 10 segundos.
                    if time.time() - timestamp > max_age_s:
                        continue

                    # Se ainda não existe nenhuma linha salva para esse UE, salva a linha atual como a mais recente.
                    if ue_id not in latest_by_ue:
                        latest_by_ue[ue_id] = row
                    else:
                        # Se já existe uma linha para esse UE, pega o timestamp da linha antiga.
                        old_timestamp = self.to_float(
                            latest_by_ue[ue_id].get("timestamp_unix"), 0
                        )
                        # Se a linha atual for mais nova que a antiga, substitui pela linha atual.
                        if timestamp > old_timestamp:
                            latest_by_ue[ue_id] = row

        except FileNotFoundError:
            print("CSV ainda nao existe: {}".format(csv_path), flush=True)

        return latest_by_ue

    def calculate_inter_slice_quota(self, metrics_by_ue):
        # Calcula a quota de PRBs para cada slice.
        #
        # Entrada:
        # metrics_by_ue -> dicionário com a última métrica de cada UE.
        #
        # Exemplo de entrada:
        # {
        #   "0": linha_csv_ue_0,
        #   "1": linha_csv_ue_1,
        #   "2": linha_csv_ue_2,
        # }
        #
        # Saída:
        # quotas -> dicionário com a quota de cada slice.
        #
        # Exemplo de saída:
        # {
        #   "SST_1_eMBB": 34,
        #   "SST_2_URLLC": 33,
        #   "SST_3_mMTC": 33,
        # }

        # Conjunto usado para guardar quais slices estão ativos.
        # set() evita repetição. Se dois UEs estão no mesmo slice, esse slice aparece só uma vez.
        active_slices = set()

        # Percorre todos os UEs que apareceram no CSV KPM.
        for csv_ue_id, row in metrics_by_ue.items():
            # O ue_id vem do CSV como string, então convertemos para inteiro.
            target_ue_id = int(csv_ue_id)

            # Primeiro tenta usar o mapa interno UE -> slice.
            # Se não encontrar, usa o slice_id que veio do CSV.
            slice_id = UE_TO_SLICE.get(target_ue_id, row.get("slice_id"))

            # Só considera o slice se ele existir no SLICE_POLICY. Isso evita usar nomes errados ou slices desconhecidos.
            if slice_id in SLICE_POLICY:
                active_slices.add(slice_id)

        # Se por algum motivo não encontrar slice ativo, usa todos os slices conhecidos como fallback.
        if not active_slices:
            active_slices = set(SLICE_POLICY.keys())

        # Ordena os slices para deixar o resultado previsível.
        active_slices = sorted(active_slices)

        # Divide 100% igualmente entre os slices ativos.
        base_quota = 100 // len(active_slices)
        remainder = 100 % len(active_slices)

        # Dicionário final de quotas por slice.
        quotas = {}

        # Monta a quota de cada slice.
        for index, slice_id in enumerate(active_slices):
            # Distribui o resto para os primeiros slices.
            # Com 3 slices: 34, 33, 33.
            quotas[slice_id] = base_quota + (1 if index < remainder else 0)

        # Retorna a quota calculada para cada slice.
        return quotas

    def calculate_intra_slice_quota(self, metrics_by_ue, slice_quota, ue_id, control_all_ues):
        # Calcula a quota final de cada UE a partir da quota de cada slice.
        #
        # Entrada:
        # metrics_by_ue    -> últimas métricas KPM por UE
        # slice_quota      -> quota já calculada por slice
        # ue_id            -> UE específico a controlar, se control_all_ues=False
        # control_all_ues  -> se True, controla todos os UEs encontrados no CSV
        #
        # Saída:
        # ue_quota -> quota final por UE.
        #
        # Exemplo:
        # {
        #   0: {
        #       "slice_id": "SST_1_eMBB",
        #       "min_prb_ratio": 0,
        #       "max_prb_ratio": 34,
        #       "dedicated_prb_ratio": 34,
        #   }
        # }

        # Primeiro vamos agrupar os UEs por slice.
        #
        # Exemplo:
        # {
        #   "SST_1_eMBB": [0, 1],
        #   "SST_2_URLLC": [2],
        # }
        ues_by_slice = {}

        # Percorre todos os UEs que apareceram nas métricas KPM.
        for csv_ue_id, row in metrics_by_ue.items():
            # Converte o UE ID do CSV para inteiro.
            target_ue_id = int(csv_ue_id)

            # Se control_all_ues for False, o xApp só deve controlar o UE passado por argumento.
            if not control_all_ues and target_ue_id != ue_id:
                continue

            #Descobre o slice do UE.
            slice_id = UE_TO_SLICE.get(target_ue_id, row.get("slice_id"))

            # Se esse slice ainda não existe no dicionário, cria uma lista vazia para ele.
            if slice_id not in ues_by_slice:
                ues_by_slice[slice_id] = []

            # Adiciona o UE na lista do seu slice.
            ues_by_slice[slice_id].append(target_ue_id)

        # Dicionário final com a quota por UE.
        ue_quota = {}

        # Depois divide a quota do slice entre os UEs daquele slice.
        for slice_id, ue_list in ues_by_slice.items():
            # Pega a quota total daquele slice.  Se não encontrar o slice em slice_quota, usa 100 como fallback.
            quota = slice_quota.get(slice_id, 100)

            # Proteção extra: se não tiver UE na lista, pula para o próximo slice.
            if not ue_list:
                continue

            # Divide a quota do slice igualmente entre os UEs daquele slice.
            quota_per_ue = quota // len(ue_list)
            remainder = quota % len(ue_list)

            #Distribui a quota entre os UEs do slice.
            for index, target_ue_id in enumerate(sorted(ue_list)):
                # Distribui o resto para os primeiros UEs.
                final_quota = quota_per_ue + (1 if index < remainder else 0)

                # Monta a política final daquele UE.
                ue_quota[target_ue_id] = {
                    "slice_id": slice_id,
                    "min_prb_ratio": 0,
                    "max_prb_ratio": final_quota,
                    "dedicated_prb_ratio": final_quota,
                }
        # Retorna as quotas finais por UE.
        return ue_quota

    def apply_rc_control(self, e2_node_id, plmn, sst, sd, ue_quota):
        # Aplica as quotas calculadas usando E2SM-RC.
        #
        # Essa função NÃO decide a política.
        # Ela apenas pega a quota final de cada UE e envia o comando RC.
        #
        # Entrada:
        # ue_quota -> dicionário com a quota final por UE.
        #
        # Exemplo:
        # {
        #   0: {
        #       "slice_id": "SST_1_eMBB",
        #       "min_prb_ratio": 0,
        #       "max_prb_ratio": 34,
        #       "dedicated_prb_ratio": 34,
        #   }
        # }
    
        # Percorre cada UE que deve receber controle.
        for target_ue_id, quota in sorted(ue_quota.items()):
            # Pega o slice do UE só para log/depuração.
            slice_id = quota.get("slice_id", "unknown")
            #Pega os valores de PRB calculados.
            min_prb_ratio = quota.get("min_prb_ratio", 0)
            max_prb_ratio = quota.get("max_prb_ratio", 100)
            dedicated_prb_ratio = quota.get("dedicated_prb_ratio", max_prb_ratio)
            
            # Horário atual para aparecer no terminal.
            current_time = datetime.datetime.now()

            # Mostra exatamente qual comando será enviado.
            print("{} Send RIC Control Request to E2 node ID: {}, UE ID/header: {}, ""slice: {}, PRB_min_ratio: {}, PRB_max_ratio: {}, ""PRB_dedicated_ratio: {}".format(
                    current_time.strftime("%H:%M:%S"),
                    e2_node_id,
                    target_ue_id,
                    slice_id,
                    min_prb_ratio,
                    max_prb_ratio,
                    dedicated_prb_ratio,
                ),
                flush=True,
            )

            # Envia o comando E2SM-RC para a gNB.
            # TODO: No setup atual, o controle efetivo acontece pelo UE ID do header. O PLMN/SST/SD seguem no payload, mas a gNB está aplicando por UE.
            self.e2sm_rc.control_slice_level_prb_quota_by_slice(e2_node_id,target_ue_id,min_prb_ratio,max_prb_ratio,dedicated_prb_ratio,plmn_string=plmn,sst=sst,sd=sd,ack_request=1,)

            
    # Mark the function as xApp start function using xAppBase.start_function decorator.
    # It is required to start the internal msg receive loop.
    @xAppBase.start_function
    def start(self, e2_node_id, ue_id, plmn, sst, sd, min_prb_ratio, max_prb_ratio, dedicated_prb_ratio, csv_path, control_period, max_metric_age_s, control_all_ues):
    #e2_node_id,              # ID do nó E2, por exemplo: gnbd_999_070_00019b_0
    #ue_id,                   # UE específico que será controlado se control_all_ues=False
    #plmn,                    # PLMN do slice, por exemplo: "99970"
    #sst,                     # SST do slice
    #sd,                      # SD do slice
    #min_prb_ratio,           # Valor mínimo de PRB permitido na política
    #max_prb_ratio,           # Valor máximo de PRB permitido na política
    #dedicated_prb_ratio,     # Valor dedicado/preferencial de PRB
    #csv_path,                # Caminho do CSV onde o xApp KPM está salvando as métricas
    #control_period,          # Intervalo, em segundos, entre comandos RC
    #max_metric_age_s,        # Idade máxima aceita para uma métrica KPM
    #control_all_ues,         # Se True, controla todos os UEs encontrados no CSV
        
        # Mostra que o xApp RC iniciou. flush=True força o print a aparecer imediatamente no terminal.
        print("[RC] xApp iniciado", flush=True)
        #Mostra qual arquivo CSV será lido.
        print("[RC] Lendo metricas KPM de: {}".format(csv_path), flush=True)

        # Guarda o instante em que o último comando RC foi enviado. Começa em 0 para permitir que o primeiro controle aconteça logo.
        last_action_time = 0

        # Loop principal do xApp.
        # self.running fica True enquanto o xApp está rodando.
        while self.running:

            # Lê o CSV KPM e pega a linha mais recente de cada UE.
            # O retorno esperado é um dicionário:
            # {
            #   "0": linha_mais_recente_do_ue_0,
            #   "1": linha_mais_recente_do_ue_1,
            #   ...
            # }
            metrics_by_ue = self.read_latest_kpm_by_ue(csv_path, max_age_s=max_metric_age_s)

            # Se não encontrou nenhuma métrica recente, espera 2 segundos e volta para o começo do loop.
            if not metrics_by_ue:
                print("[RC] Nenhuma metrica KPM recente encontrada", flush=True)
                time.sleep(2)
                continue
                    
            # Verifica se já passou tempo suficiente desde o último controle. Isso evita enviar comandos RC a cada leitura do CSV.
            if time.time() - last_action_time >= control_period:
                
                # 3. Calcula a política inter-slice uma vez por rodada.
                # Essa função olha o estado geral da rede.  Hoje pode dividir igualmente.  Depois pode usar throughput médio, delay médio, prioridade, etc.
                slice_quota = self.calculate_inter_slice_quota(metrics_by_ue)

                ue_quota = self.calculate_intra_slice_quota(metrics_by_ue,slice_quota,ue_id,control_all_ues)

                print("[POLICY][INTER] Quotas por slice: {}".format(slice_quota), flush=True)

                self.apply_rc_control(e2_node_id,plmn,sst,sd,ue_quota)

                last_action_time = time.time()

                # Pequena pausa para não ficar lendo o CSV em loop apertado.
                time.sleep(5)


# Esse bloco só roda quando o arquivo é executado diretamente.
# Se esse arquivo for importado por outro script, essa parte não executa.
if __name__ == '__main__':
    # Cria o parser de argumentos de linha de comando.
    parser = argparse.ArgumentParser(description='My example xApp')

    # Argumentos básicos do xApp.
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")

    # Identificação do nó E2 e da RAN Function RC.
    parser.add_argument("--e2_node_id", type=str, default='gnbd_001_001_00019b_0', help="E2 Node ID")
    parser.add_argument("--ran_func_id", type=int, default=3, help="E2SM RC RAN function ID")

    # UE que será controlado caso control_all_ues não seja usado.
    parser.add_argument("--gnb_cu_ue_f1ap_id", type=int, default=0, help="UE ID")

        # Identificação do slice.

    parser.add_argument("--plmn", type=str, default="99970", help="PLMN do slice")
    parser.add_argument("--sst", type=int, default=1, help="SST do slice")
    parser.add_argument("--sd", type=int, default=1, help="SD do slice")

    # Parâmetros de controle PRB.
    parser.add_argument("--min_prb_ratio", type=int, default=0, help="Min PRB Policy Ratio")
    parser.add_argument("--max_prb_ratio", type=int, default=100, help="Max PRB Policy Ratio")
    parser.add_argument("--dedicated_prb_ratio", type=int, default=100, help="Dedicated PRB Policy Ratio")

    # Caminho do CSV gerado pelo xApp KPM.
    parser.add_argument("--csv_path", type=str, default="/opt/xApps/data/kpm_metrics.csv", help="Caminho do CSV KPM")

    # Intervalo entre comandos RC.
    parser.add_argument("--control_period", type=int, default=30, help="Intervalo em segundos entre comandos RC")

    # Idade máxima aceita para uma métrica. Se uma linha do CSV for mais antiga que isso, ela é ignorada.
    parser.add_argument("--max_metric_age_s", type=int, default=10, help="Idade maxima das metricas KPM")

    # Flag booleana. Se aparecer na linha de comando, vira True. Se não aparecer, fica False.
    parser.add_argument("--control_all_ues", action="store_true", help="Controla todos os UEs encontrados no CSV")

    # Lê os argumentos passados pelo terminal.
    args = parser.parse_args()

    # Cria o objeto do xApp.
    myXapp = MyXapp(args.config, args.http_server_port, args.rmr_port)
    # Define qual RAN Function ID será usada para E2SM-RC.  No seu setup, RC costuma ser ran_func_id=3.
    myXapp.e2sm_rc.set_ran_func_id(args.ran_func_id)
    

    # Configura sinais para encerrar o xApp corretamente.  Assim, Ctrl+C ou kill tenta finalizar de forma limpa.    
    # signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    # Inicia o xApp passando todos os argumentos para a função start().
    myXapp.start(args.e2_node_id, args.gnb_cu_ue_f1ap_id, args.plmn, args.sst, args.sd, args.min_prb_ratio, args.max_prb_ratio, args.dedicated_prb_ratio, args.csv_path, args.control_period, args.max_metric_age_s, args.control_all_ues,)
