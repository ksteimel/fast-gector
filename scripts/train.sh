#!/bin/bash
set -x
detect_vocab_path="./data/vocabulary/d_tags.txt"
correct_vocab_path="./data/vocabulary/labels.txt"
train_path="gec_private_train_data/made_up.edits"
valid_path="gec_private_train_data/made_up.edits"
config_path="configs/ds_config_zero1_fp16.json"
timestamp=`date "+%Y%0m%0d_%T"`
save_dir="ckpts/ckpt_$timestamp"
tensorboard_dir="logs/tb/gector_${timestamp}"
pretrained_transformer_path="roberta-base"
mkdir -p $save_dir
cp $0 $save_dir
cp $config_path $save_dir

python train.py \
    --num_epochs 10 \
    --max_num_tokens 128 \
    --train_batch_size 16\
    --gradient_accumulation_steps 4\
    --valid_batch_size 256 \
    --cold_step_count 0 \
    --warmup 0.1 \
    --cold_lr 1e-3 \
    --skip_correct 0 \
    --skip_complex 0 \
    --sub_token_mode average \
    --special_tokens_fix 1 \
    --unk2keep 0 \
    --tp_prob 1 \
    --tn_prob 1 \
    --detect_vocab_path $detect_vocab_path \
    --correct_vocab_path $correct_vocab_path \
    --do_eval \
    --train_path $train_path \
    --valid_path $valid_path \
    --save_dir $save_dir \
    --use_cache 0 \
    --log_interval 1 \
    --eval_interval 50 \
    --save_interval 50 \
    --pretrained_transformer_path $pretrained_transformer_path\
    --tensorboard_dir $tensorboard_dir 2>&1

