from model.tools import *


import torch, sys, os, tqdm, numpy, soundfile, time, pickle
import torch.nn as nn
#from tools import *
from model.loss import AAMsoftmax
from model.model import ECAPA_TDNN

class ECAPAModel(nn.Module):
    def __init__(self, lr, lr_decay, C, n_class, m, s, test_step, device):
        super(ECAPAModel, self).__init__()
        ## ECAPA-TDNN
        self.speaker_encoder = ECAPA_TDNN(C=C).to(device)
        ## Classifier
        self.speaker_loss = AAMsoftmax(n_class=n_class, m=m, s=s).to(device)

        self.optim = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=2e-5)
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optim, step_size=test_step, gamma=lr_decay)
        print(time.strftime("%m-%d %H:%M:%S") + " Model para number = %.2f" % (sum(param.numel() for param in self.speaker_encoder.parameters()) / 1024 / 1024))

    def forward(self, x, aug=False):
        # 입력 데이터를 받아 임베딩을 생성하는 부분
        embeddings = self.speaker_encoder(x, aug=aug)
        return embeddings

    def train_network(self, epoch, loader):
        self.train()
        ## Update the learning rate based on the current epoch
        self.scheduler.step(epoch - 1)
        index, top1, loss = 0, 0, 0
        lr = self.optim.param_groups[0]['lr']

        # 전체 배치 수 계산
        batch_count = len(loader)

        for num, (data, labels) in enumerate(loader, start=1):
            self.zero_grad()
            labels = torch.LongTensor(labels).cuda()
            speaker_embedding = self(data.cuda(), aug=True)  # forward() 대신 직접 호출
            nloss, prec = self.speaker_loss(speaker_embedding, labels)
            nloss.backward()
            self.optim.step()

            index += len(labels)

            # prec가 텐서 또는 리스트인 경우에 대응
            if isinstance(prec, torch.Tensor):
                prec = prec.item()  # 텐서인 경우 item() 호출
            elif isinstance(prec, list):
                prec = sum(prec) / len(prec)  # 리스트인 경우 평균 계산

            top1 += prec  # 정확도를 누적

            loss += nloss.item()  # detach 후 numpy 변환 대신 item 사용

            # 각 배치마다 손실(Loss) 및 정확도 출력
            sys.stderr.write(time.strftime("%m-%d %H:%M:%S") + \
                             " [%2d] Lr: %5f, Training: %.2f%%, " % (epoch, lr, 100 * (num / batch_count)) + \
                             " Loss: %.5f, ACC: %2.2f%% \r" % (loss / num, top1 / index * len(labels)))
            sys.stderr.flush()

        sys.stdout.write("\n")
        avg_loss = loss / num
        avg_acc = top1 / index  # 평균 정확도 계산
        return avg_loss, lr, avg_acc

    def eval_network(self, eval_list, eval_path):
        self.eval()
        files = []
        embeddings = {}
        lines = open(eval_list).read().splitlines()
        for line in lines:
            files.append(line.split()[1])
            files.append(line.split()[2])
        setfiles = list(set(files))
        setfiles.sort()

        for idx, file in tqdm.tqdm(enumerate(setfiles), total=len(setfiles)):
            audio, _ = soundfile.read(os.path.join(eval_path, file))
            # Full utterance
            data_1 = torch.FloatTensor(numpy.stack([audio], axis=0)).cuda()

            # Split utterance matrix
            max_audio = 300 * 160 + 240
            if audio.shape[0] <= max_audio:
                shortage = max_audio - audio.shape[0]
                audio = numpy.pad(audio, (0, shortage), 'wrap')
            feats = []
            startframe = numpy.linspace(0, audio.shape[0] - max_audio, num=5)
            for asf in startframe:
                feats.append(audio[int(asf):int(asf) + max_audio])
            feats = numpy.stack(feats, axis=0).astype(float)
            data_2 = torch.FloatTensor(feats).cuda()

            # Speaker embeddings
            with torch.no_grad():
                embedding_1 = self(data_1, aug=False)
                embedding_1 = nn.functional.normalize(embedding_1, p=2, dim=1)
                embedding_2 = self(data_2, aug=False)
                embedding_2 = nn.functional.normalize(embedding_2, p=2, dim=1)
            embeddings[file] = [embedding_1, embedding_2]

        scores, labels = [], []

        for line in lines:
            embedding_11, embedding_12 = embeddings[line.split()[1]]
            embedding_21, embedding_22 = embeddings[line.split()[2]]
            # Compute the scores
            score_1 = torch.mean(torch.matmul(embedding_11, embedding_21.T))  # higher is positive
            score_2 = torch.mean(torch.matmul(embedding_12, embedding_22.T))
            score = (score_1 + score_2) / 2
            score = score.detach().cpu().numpy()
            scores.append(score)
            labels.append(int(line.split()[0]))

        # Compute EER and minDCF
        EER = tuneThresholdfromScore(scores, labels, [1, 0.1])[1]
        fnrs, fprs, thresholds = ComputeErrorRates(scores, labels)
        minDCF, _ = ComputeMinDcf(fnrs, fprs, thresholds, 0.05, 1, 1)

        return EER, minDCF

    def save_parameters(self, path):
        torch.save(self.state_dict(), path)

    def load_parameters(self, path):
        self_state = self.state_dict()
        loaded_state = torch.load(path)
        for name, param in loaded_state.items():
            origname = name
            if name not in self_state:
                name = name.replace("module.", "")
                if name not in self_state:
                    print("%s is not in the model." % origname)
                    continue
            if self_state[name].size() != loaded_state[origname].size():
                print("Wrong parameter length: %s, model: %s, loaded: %s" % (origname, self_state[name].size(), loaded_state[origname].size()))
                continue
            self_state[name].copy_(param)
