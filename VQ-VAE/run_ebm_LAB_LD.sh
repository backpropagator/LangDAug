CUDA_VISIBLE_DEVICES=0 python adapt_fundus_LAB_LD.py \
    --n_gpu 1 \
    --dataset 'fundus' \
    --source '1' \
    --target '2' \
    --channel_mul 8 \
    --langevin_step 30 \
    --langevin_lr 10.0 \
    --lr 0.001 \
    --beta1 0.5 \
    --beta2 0.999 \
    --attention \
    --n_embed 512 \
    --embed_dim 64 \
    --batch_size 8 \
    --sn \
    --ae_ckpt /path/to/vqvae_best.pt \
    --data_root ./datasets/ \
    --num_save 5 \
    --color_space LAB \
    --expt_name train_LAB_LD_fundus
