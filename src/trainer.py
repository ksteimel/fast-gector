# -*- coding:UTF-8 -*-
import torch
from transformers import AutoTokenizer
from transformers.optimization import get_linear_schedule_with_warmup
from utils.mismatched_utils import *
from utils.data_utils import init_dataloader
from src.dataset import Seq2EditVocab
from utils.helpers import INCORRECT_LABEL, KEEP_LABEL, PAD_LABEL, START_TOKEN
from src.model import GECToRModel
from tqdm import tqdm
from sklearn.metrics import accuracy_score
from random import seed
import os
import json
from torch.utils.tensorboard import SummaryWriter
from loguru import logger

class Trainer:
    def __init__(self, args):
        print("Init trainer")
        self.fix_seed()
        self.device = self.setup_device(args.local_rank)
        self.n_gpus = torch.cuda.device_count()
        self.log_interval = args.log_interval
        self.eval_interval = args.eval_interval
        self.train_batch_size = args.train_batch_size
        self.gradient_accumulation_steps = args.gradient_accumulation_steps
        # to ensure each process has a summary writer
        self.summary_writer = None
        self.num_epochs = args.num_epochs
        self.valid_batch_size = args.valid_batch_size # 
        self.do_eval = args.do_eval
        self.cold_lr = args.cold_lr
        self.cold_step_count = args.cold_step_count
        self.max_num_tokens = args.max_num_tokens
        self.max_pieces_per_token = args.max_pieces_per_token
        self.tp_prob = args.tp_prob
        self.tn_prob = args.tn_prob
        self.tag_strategy = args.tag_strategy
        self.skip_complex = bool(args.skip_complex)
        self.skip_correct = bool(args.skip_correct)
        self.train_path = args.train_path
        self.valid_path = args.valid_path
        self.use_cache = bool(args.use_cache)
        self.model_dir = args.model_dir
        self.ckpt_id = args.ckpt_id
        self.save_dir = args.save_dir
        self.vocab = Seq2EditVocab(
            args.detect_vocab_path, args.correct_vocab_path, unk2keep=bool(args.unk2keep))
        self.base_tokenizer = AutoTokenizer.from_pretrained(
            args.pretrained_transformer_path, do_basic_tokenize=False)
        self.base_tokenizer_vocab = self.base_tokenizer.get_vocab()
        if bool(args.special_tokens_fix):  # for roberta
            self.base_tokenizer.add_tokens([START_TOKEN], special_tokens=True)
            # set start_token to unk_token_id is no longer supported via transformers tokenizer
            # since access the vocab is implemented by calling get_vocab() which create a new instance,
            # in this case, we cannot actually change the vocab.
            # Instead, we can get the vocab and change it, then use it directly later on.
            # self.base_tokenizer.vocab[START_TOKEN] = self.base_tokenizer.unk_token_id
            self.base_tokenizer_vocab[START_TOKEN] = self.base_tokenizer.unk_token_id
        self.mismatched_tokenizer = MisMatchedTokenizer(
            self.base_tokenizer, self.base_tokenizer_vocab, self.max_pieces_per_token)

        model = GECToRModel(
            encoder_path=args.pretrained_transformer_path,
            num_detect_tags=len(self.vocab.detect_vocab["id2tag"]),
            num_correct_tags=len(self.vocab.correct_vocab["id2tag"]),
            additional_confidence=args.additional_confidence,
            dp_rate=args.dp_rate,
            detect_pad_id=self.vocab.detect_vocab["tag2id"][PAD_LABEL],
            correct_pad_id=self.vocab.correct_vocab["tag2id"][PAD_LABEL],
            detect_incorrect_id=self.vocab.detect_vocab["tag2id"][INCORRECT_LABEL],
            correct_keep_id=self.vocab.correct_vocab["tag2id"][KEEP_LABEL],
            sub_token_mode=args.sub_token_mode,
            device=self.device
        )

        self.train_loader = init_dataloader(
            subset="train",
            data_path=self.train_path,
            num_workers=args.num_workers,
            use_cache=self.use_cache,
            tokenizer=self.mismatched_tokenizer,
            vocab=self.vocab,
            input_pad_id=self.base_tokenizer.pad_token_id,
            detect_pad_id=self.vocab.detect_vocab["tag2id"][PAD_LABEL],
            correct_pad_id=self.vocab.correct_vocab["tag2id"][PAD_LABEL],
            max_num_tokens=self.max_num_tokens,
            batch_size=int(self.train_batch_size // self.gradient_accumulation_steps // self.n_gpus),
            tag_strategy=self.tag_strategy,
            skip_complex=self.skip_complex,
            skip_correct=self.skip_correct,
            tp_prob=self.tp_prob,
            tn_prob=self.tn_prob)
        logger(f"# training dataset: {len(self.train_loader.dataset)}", ranks=[0])
        self.valid_loader = None
        if args.do_eval:
            self.valid_loader = init_dataloader(
                subset="valid",
                data_path=self.valid_path,
                use_cache=self.use_cache,
                num_workers=args.num_workers,
                tokenizer=self.mismatched_tokenizer,
                vocab=self.vocab,
                input_pad_id=self.base_tokenizer.pad_token_id,
                detect_pad_id=self.vocab.detect_vocab["tag2id"][PAD_LABEL],
                correct_pad_id=self.vocab.correct_vocab["tag2id"][PAD_LABEL],
                max_num_tokens=self.max_num_tokens,
                batch_size=int(self.train_batch_size // self.n_gpus),
                tag_strategy=self.tag_strategy,
                skip_complex=self.skip_complex,
                skip_correct=self.skip_correct,
                tp_prob=self.tp_prob,
                tn_prob=self.tn_prob)
            logger(f"# validation dataset: {len(self.valid_loader.dataset)}", ranks=[0])

        self.total_training_steps = int(len(self.train_loader) // self.gradient_accumulation_steps * self.num_epochs)
        # if save interval is not set by args, save at the end of each epoch
        self.save_interval = args.save_interval if args.save_interval is not None else len(self.train_loader) // self.gradient_accumulation_steps
        logger(f"set total training steps to {self.total_training_steps}", ranks=[0])
        self.model, self.optimizer, self.lr_scheduler = \
            self.setup_model_optimizer_and_scheduler(
                                                    model=model, 
                                                    total_training_steps=self.total_training_steps,
                                                    warmup=args.warmup)
        
        self.best_accuracy = 0
        self.best_global_step = 0
        self.best_loss = float("inf")

    def import_ds_config_hyper_params(self, config_path):
        with open(config_path, "r", encoding="utf8") as fr:
            config = json.load(fr)
        self.train_batch_size = config.get("train_batch_size")
        self.gradient_accumulation_steps = config.get("gradient_accumulation_steps")
        self.lr = config["optimizer"].get("lr")

    def setup_device(self, local_rank=-1):
        if torch.cuda.is_available():
            if comm.is_initialized() and local_rank != -1:
                device = torch.device("cuda", local_rank)
            else:
                device = torch.device("cuda")
        else:
            device = torch.device("cpu")
        return device

    def init_scheduler(self, optimizer, total_train_steps, warmup_ratio):
        torch_optimizer = optimizer
        lr_scheduler = get_linear_schedule_with_warmup(
            optimizer=torch_optimizer,
            num_warmup_steps=int(total_train_steps * warmup_ratio),
            num_training_steps=total_train_steps,
        )
        logger(f"setup lr_scheduler {lr_scheduler}", ranks=[0])
        return lr_scheduler

    def setup_model_optimizer_and_scheduler(self, model, total_training_steps: int, warmup: float):
        lr_scheduler = self.init_scheduler(optimizer=optimizer,
                                        total_train_steps=total_training_steps,
                                        warmup_ratio=warmup)
        # load ckpt and reset lr
        if self.model_dir and self.ckpt_id:
            model.load_checkpoint(self.model_dir, self.ckpt_id)
            logger(f"load model from {self.model_dir}", ranks=[0])
            for param_group in optimizer.param_groups:
                param_group['lr'] = self.lr

        else:
            logger("no model checkpoint found, train from beginning...", ranks=[0])
        return model, optimizer, lr_scheduler

    def train(self):
        if not os.path.exists(self.save_dir):
            os.mkdir(self.save_dir)

        self.encoder_requires_grad = True
        global_train_step = 0 # init a global step
        for epoch in range(self.num_epochs):
            if isinstance(self.train_loader.sampler, torch.utils.data.DistributedSampler):
                self.train_loader.sampler.set_epoch(epoch)
            self.model.train()
            if self.cold_step_count:

                if epoch < self.cold_step_count:
                    for param_group in self.optimizer.param_groups:
                        param_group['lr'] = self.cold_lr
                    self.encoder_requires_grad = False
                else:
                    if self.encoder_requires_grad == False:
                        torch.clear_autocast_cache()
                        torch.cuda.empty_cache()
                        for param_group in self.optimizer.param_groups:
                            param_group['lr'] = self.lr
                        self.encoder_requires_grad = True
            train_loss, global_train_step = self._train_epoch(global_train_step)
            

            

    def _save_ckpt(self, global_step):
        self.model.save_checkpoint(self.save_dir, f"globalstep-{global_step}")

    def _save_metric(self, global_step, metrics):
        with open(os.path.join(self.save_dir, f"metrics_globalstep-{global_step}.json"), "w", encoding="utf8") as fw:
            fw.write(json.dumps(metrics, ensure_ascii=False, indent=2))

    def _train_epoch(self, global_train_step):
        epoch_loss = 0
        num_steps_per_epoch = len(self.train_loader) // self.gradient_accumulation_steps
        # drop last step at the end of training 
        # if the last step cannot accumulate the same num of batches as before 
        # in order to keep the global batch size unchanged
        if len(self.train_loader) % self.gradient_accumulation_steps > 0:
            drop_last_step = True
        else:
            drop_last_step = False

        pbar = tqdm(total=num_steps_per_epoch)
        step = 0
        for batch in self.train_loader:
            for k, v in batch.items():
                batch[k] = v.to(self.device)
            outputs = self.model(batch, self.encoder_requires_grad)

            loss = outputs["loss"]
            if self.n_gpus > 1:
                loss = loss.mean() # mean across gpus
            if self.gradient_accumulation_steps > 1:
                loss = loss / self.gradient_accumulation_steps # loss avg across gradient accumulation steps
            self.model.backward(loss)
            self.model.step()
            loss_i = loss.detach().item()
            epoch_loss += loss_i
            if self.model.is_gradient_accumulation_boundary():
                if self.encoder_requires_grad == True:
                    self.lr_scheduler.step()
                    current_lr = self.lr_scheduler.get_last_lr()[0]
                else:
                    current_lr = self.cold_lr
                
                if (step + 1) % self.log_interval == 0 or step == num_steps_per_epoch - 1:
                    info = {'Loss/train': loss_i, 'lr': current_lr}
                    pbar.set_postfix(info)
                    if step == num_steps_per_epoch - 1:
                        update_steps = step % self.log_interval
                    else:
                        update_steps = self.log_interval
                    pbar.update(update_steps)
                    if self.summary_writer is not None:
                        for metric, value in info.items():
                            self.summary_writer.add_scalar(metric, value, global_step=global_train_step)
                if step >= num_steps_per_epoch - 1 or (step == num_steps_per_epoch -2 and drop_last_step):
                    break
                
                if self.do_eval and (global_train_step + 1) % self.eval_interval == 0:
                    self.model.eval()
                    valid_loss, valid_acc = self.evaluate()
                    comm.barrier() # sync here to make sure all ranks have metrics
                    if self.summary_writer is not None:
                        self.summary_writer.add_scalar("Loss/valid", valid_loss, global_train_step)
                        self.summary_writer.add_scalar("Acc/valid", valid_acc, global_train_step)
                    metrics = {"current_global_step": global_train_step, "current_train_loss": epoch_loss / (step + 1),
                        "valid_loss": valid_loss, "valid_accuracy": valid_acc}
                    if comm.get_rank() == 0:
                        if valid_loss < self.best_loss:
                            self.best_loss = valid_loss
                        if valid_acc > self.best_accuracy:
                            self.best_accuracy = valid_acc
                            self.best_global_step = global_train_step
                        metrics["best_global_step"] = self.best_global_step
                        metrics["best_valid_loss"] = self.best_loss
                        metrics["best_valid_accuracy"] = self.best_accuracy
                        self._save_metric(global_train_step, metrics)
                        logger(f"evaluation results: {metrics}", ranks=[0])
                if (global_train_step + 1) % self.save_interval == 0:
                    self._save_ckpt(global_train_step)
                step += 1
                global_train_step += 1
        epoch_loss /= num_steps_per_epoch
        return epoch_loss, global_train_step

    def evaluate(self):

        valid_loss = 0
        all_pred_labels = list()
        all_gold_labels = list()
        with torch.no_grad():
            for batch in tqdm(self.valid_loader):
                for k, v in batch.items():
                    batch[k] = v.to(self.device)
                outputs = self.model(batch)
                loss = outputs["loss"]
                if self.n_gpus > 1:
                    loss = loss.mean()
                valid_loss += loss.detach()
                batch_word_mask = batch["word_mask"].cpu().bool()
                batch_pred_label_probs = outputs["class_probabilities_labels"].detach(
                ).cpu()
                batch_pred_labels = torch.argmax(
                    batch_pred_label_probs, dim=-1)
                batch_pred_labels = torch.masked_select(
                    batch_pred_labels, batch_word_mask).tolist()
                all_pred_labels.extend(batch_pred_labels)
                batch_gold_labels = torch.masked_select(
                    batch["correct_tag_ids"].cpu(), batch_word_mask).tolist()
                all_gold_labels.extend(batch_gold_labels)
            valid_loss /= len(self.valid_loader)
            acc = torch.tensor(accuracy_score(all_gold_labels, all_pred_labels), dtype=torch.float64).cuda()
            # all reduce across dp to get full metrics
            if comm.is_initialized() and comm.get_world_size() > 1:
                comm.all_reduce(valid_loss, op=comm.ReduceOp.AVG, group=_get_data_parallel_group())
                comm.all_reduce(acc, op=comm.ReduceOp.AVG, group=_get_data_parallel_group())

        valid_loss = valid_loss.item()
        acc = acc.item()
        return valid_loss, acc

    def fix_seed(self):
        torch.manual_seed(1)
        torch.backends.cudnn.enabled = True
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = True
        seed(43)
