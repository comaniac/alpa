"""Benchmark one case of inter-op + intra-op parallelism."""
import time

import jax
import jax.numpy as jnp
import numpy as np
import optax

# import alpa
# from alpa import (global_config, mark_pipeline, manual_layer_construction,
#                   automatic_layer_construction, automatic_remat)
from alpa.model.bert_model import BertConfig
from alpa.model.gpt_model import FlaxGPTForLMModule
from alpa.model.model_util import TrainState
from alpa.util import print_used_time
from benchmark.util import compute_gpt_parameter_count, compute_gpt_tflops
from examples.opt.suite_opt import opt_specs

# as_option = global_config.default_autosharding_option


def create_train_state(rngkey, model, batch, dtype):
    params = model.init_dummy(rngkey, batch["input_ids"], batch["attention_mask"],
                              batch["token_type_ids"], batch["position_ids"])

    def weight_decay_mask(pytree):
        # do not use weight decay on layer norm and bias.
        return jax.tree_map(lambda x: x.ndim > 1, pytree)

    tx = optax.chain(
        #optax.clip_by_global_norm(1.0),  # TODO(lmzheng): fix reduce-scatter for this
        optax.adamw(learning_rate=1e-2, mask=weight_decay_mask)
    )
    mixed_precision = (dtype == jnp.float16)
    state = TrainState.create(
        apply_fn=model.apply,
        params=params,
        tx=tx,
        mixed_precision=mixed_precision,
        dynamic_scale=None)
    return state


def create_train_state_aval(rngkey, model, batch, dtype):
    params = jax.eval_shape(model.init, rngkey, batch["input_ids"],
                            batch["attention_mask"], batch["token_type_ids"],
                            batch["position_ids"])

    def weight_decay_mask(pytree):
        # do not use weight decay on layer norm and bias.
        return jax.tree_map(lambda x: x.ndim > 1, pytree)

    tx = optax.chain(
        #optax.clip_by_global_norm(1.0),  # TODO(lmzheng): fix reduce-scatter for this
        optax.adamw(learning_rate=1e-2, mask=weight_decay_mask)
    )
    mixed_precision = (dtype == jnp.float16)
    state = TrainState.create_aval(
        apply_fn=model.apply,
        params=params,
        tx=tx,
        mixed_precision=mixed_precision,
        dynamic_scale=None)
    return state


def get_train_step(auto_layer,
                   num_manual_pipeline_stages=None,
                   num_auto_layers=None,
                   auto_remat_mode=None,
                   num_auto_remat_layers=None):

    # @parallelize
    def train_step(state, batch, rng_key):

        def loss_func(params):
            rngs = {"dropout": rng_key}
            logits = state.apply_fn(params,
                                    batch["input_ids"],
                                    batch["attention_mask"],
                                    batch["token_type_ids"],
                                    batch["position_ids"],
                                    deterministic=True,
                                    rngs=rngs)[0]
            label_mask = jnp.where(batch["labels"]  > 0, 1.0, 0.0)
            labels = jax.nn.one_hot(batch["labels"], logits.shape[-1])
            loss = - jnp.sum(labels * jax.nn.log_softmax(logits, axis=-1), axis=-1)
            loss = (label_mask * loss).sum() / label_mask.sum()
            return loss

        grads = jax.grad(loss_func)(state.params)
        new_state = state.apply_gradients(grads=grads)
        # TODO(lmzheng): add dynamic scaling for mixed-precision training
        return new_state

    return train_step


def benchmark_opt_internal(benchmark_case, niter, aval_train_state=True):
    print_used_time(None)

    # Model configs
    seq_len, hidden_size, num_layers, num_heads, vocab_size = benchmark_case
    dtype = jnp.float16
    tie_word_embeddings = True
    batch_size = 1024

    # # Connect to the cluster
    # device_cluster = DeviceCluster()
    # virtual_mesh = device_cluster.get_virtual_physical_mesh(
    #     host_ids=list(range(num_hosts)),
    #     num_devices_per_host=num_devices_per_host)

    # # Parallel configs
    # if parallel_mode == "search":
    #     prefer_reduce_scatter, use_remat, num_auto_layers, overwrite_global_config_dict = parallel_args
    #     auto_layer = True
    #     auto_remat_mode = "coarse_grained" if use_remat else None
    #     num_auto_remat_layers = None
    #     add_manual_layer_marker = add_manual_remat = num_manual_pipeline_stages = False
    #     set_parallelize_options(devices=virtual_mesh,
    #                             strategy="pipeshard_parallel",
    #                             pipeline_stage_mode="auto_stage",
    #                             num_micro_batches=num_micro_batches)
    #     global_config.update_with_dict(overwrite_global_config_dict)
    # elif parallel_mode == "load_solution":
    #     (prefer_reduce_scatter, use_remat, num_auto_layers, forward_stage_layer_ids,
    #      sub_physical_mesh_shapes, sub_logical_mesh_shapes,
    #      submesh_autosharding_option_dicts) = parallel_args
    #     auto_layer = True
    #     auto_remat_mode = "fine_grained" if use_remat else None
    #     num_auto_remat_layers = num_layers
    #     add_manual_layer_marker = add_manual_remat = num_manual_pipeline_stages = False
    #     set_parallelize_options(devices=virtual_mesh,
    #                             strategy="pipeshard_parallel",
    #                             pipeline_stage_mode="manual_stage",
    #                             num_micro_batches=num_micro_batches,
    #                             forward_stage_layer_ids=forward_stage_layer_ids,
    #                             sub_physical_mesh_shapes=sub_physical_mesh_shapes,
    #                             sub_logical_mesh_shapes=sub_logical_mesh_shapes,
    #                             submesh_autosharding_option_dicts=submesh_autosharding_option_dicts)
    # elif parallel_mode == "manual":
    #     (prefer_reduce_scatter, use_remat, (dp, op, pp),
    #         force_batch_dim_mapping) = parallel_args
    #     if force_batch_dim_mapping:
    #         as_option.force_batch_dim_to_mesh_dim = 0
    #     auto_layer = False
    #     num_auto_layers = auto_remat_mode = num_auto_remat_layers = None
    #     add_manual_layer_marker = True
    #     add_manual_remat = use_remat
    #
    #     logical_mesh_shape = (dp, op)
    #     num_manual_pipeline_stages = pp
    #     num_mesh_devices = np.prod(logical_mesh_shape)
    #     num_devices_per_host = 8
    #     physical_mesh_shape = (
    #         (num_mesh_devices + num_devices_per_host - 1) // num_devices_per_host,
    #         num_mesh_devices % num_devices_per_host)
    #
    #     set_parallelize_options(devices=virtual_mesh,
    #                             strategy="pipeshard_parallel",
    #                             pipeline_stage_mode="manual_stage",
    #                             num_micro_batches=num_micro_batches,
    #                             forward_stage_layer_ids=[[i] for i in range(pp)],
    #                             sub_physical_mesh_shapes=[physical_mesh_shape] * pp,
    #                             sub_logical_mesh_shapes=[logical_mesh_shape] * pp,
    #                             submesh_autosharding_option_dicts=[{}] * pp)
    # else:
    #     raise ValueError(f"Invalid model: {parallel_mode}")

    # as_option.prefer_reduce_scatter = prefer_reduce_scatter

    # Prepare input batch
    batch = {
        "input_ids": jnp.ones((batch_size, seq_len), dtype=jnp.int32),
        "attention_mask": jnp.ones((batch_size, seq_len), dtype=jnp.int32),
        "token_type_ids": jnp.ones((batch_size, seq_len), dtype=jnp.int32),
        "position_ids": jnp.ones((batch_size, seq_len), dtype=jnp.int32),
        "labels": jnp.ones((batch_size, seq_len), dtype=jnp.int32),
    }
    print_used_time("Prepare input")

    # Init train state

    model = FlaxGPTForLMModule(BertConfig(
        vocab_size=vocab_size,
        hidden_size=hidden_size,
        num_attention_heads=num_heads,
        intermediate_size=hidden_size * 4,
        num_hidden_layers=num_layers,
        type_vocab_size=0,
        tie_word_embeddings=tie_word_embeddings,
        gradient_checkpointing=False,
        add_manual_pipeline_markers=False,
        pipeline_mp_size=0,
    ), dtype=dtype)


    rngkey = jax.random.PRNGKey(0)
    if aval_train_state:
        state = create_train_state_aval(rngkey, model, batch, dtype)
    else:
        state = create_train_state(rngkey, model, batch, dtype)
    print_used_time("Create train state")

    # Compile executable
    train_step = get_train_step(True)
    # executable = train_step.get_executable(state, batch, rngkey)
    # print_used_time("Compile (driver)")

    # if parallel_mode == "search":
    #     compilation_times = {k : timers(k).elapsed() for k in
    #             ["stage-construction", "stage-construction-dp",
    #              "stage-construction-compilation", "stage-construction-profiling"]}
    #     print(f"compilation time breakdown: {to_str_round(compilation_times, 2)}")
    # else:
    #     compilation_times = None

    # Dump hlo ir for debugging
    # stage_hlo_texts = executable.get_hlo_text()
    # for i in range(len(stage_hlo_texts)):
    #     with open(f"tmp/stage_{i}.hlo", "w") as fout:
    #         fout.write(stage_hlo_texts[i])
    # with open(f"tmp/resharding_tasks.txt", "w") as fout:
    #     fout.write(executable.print_resharding_tasks())

    # executable.sync()
    # print_used_time("Compile (worker)")

    latencies = []
    # Benchmark latency without driver overhead
    for i in range(niter):
        print(f"Iteration {i} ...")
        tic = time.time()
        state = train_step(state, batch, rngkey)
        tok = time.time()
        latencies.append(tok - tic)
        # executable.sync()

    # latencies = executable.get_execution_time_costs(warmup=1)
    # max_mem_allocated = executable.get_max_memory_allocated()

    # # Benchmark latency with driver overhead
    # if False:
    #     global_config.use_dummy_value_for_benchmarking = False
    #     global_config.pipeline_sync_for_timer = False
    #     number = niter
    #     executable.sync()
    #     tic = time.time()
    #     for i in range(number):
    #         state = train_step(state, batch, rngkey)
    #     executable.sync()
    #     e2e_latency = (time.time() - tic) / number
    #     print(f"latency with dirver overhead: {e2e_latency:.3f}")
    # print_used_time("Benchmark")

    # Compute statistics
    tflops = compute_gpt_tflops(batch_size, seq_len, num_layers,
                                hidden_size, vocab_size,
                                1,
                                np.mean(latencies))
    tflops_ckpt = compute_gpt_tflops(batch_size, seq_len, num_layers,
                                     hidden_size, vocab_size,
                                     1,
                                     np.mean(latencies), True)
    parameter_count = compute_gpt_parameter_count(num_layers, hidden_size, vocab_size)
    #report_pipeline_breakdown(executable, ["resharding_send", "resharding_recv", "compute"], niter)
    # executable.shutdown()
    # return (parameter_count, max_mem_allocated, latencies,
    #         tflops, tflops_ckpt, compilation_times) + get_last_dp_result()
    return (parameter_count, latencies, tflops)

if __name__ == "__main__":
    benchmark_opt_internal(opt_specs["350M"], 5, False)