-- /etc/slurm/job_submit.lua

function slurm_job_submit(job_desc, part_list, submit_uid)

    -- define target partition
    local quantum_partition = "quantum"

    -- Check whether the target partition is the intended submission destination.
    if job_desc.partition ~= quantum_partition then
        return slurm.SUCCESS
    end

    -- Check whether licenses are specified.
    local licenses = job_desc.licenses
    if licenses == nil or licenses == "" then
        slurm.log_user("Error: Jobs on partition '%s' require --licenses=ibm_kingston@slurmdb:1",
            quantum_partition)
        return slurm.ERROR
    end

    local script = job_desc["script"]
    local qpu = string.match(script, "#SBATCH%s+--qpu=(%S+)")
    if qpu then
        slurm.log_info("detected qpu: %s", qpu)
    end

    -- Check whether ibm_kingston@slurmdb is included.
    if not string.find(licenses, "ibm_kingston@slurmdb") then
        slurm.log_user("Error: Jobs on partition '%s' require --licenses=ibm_kingston@slurmdb:1",
            quantum_partition)
        return slurm.ERROR
    end

    return slurm.SUCCESS
end

function slurm_job_modify(job_desc, job_rec, part_list, modify_uid)
    return slurm.SUCCESS
end
