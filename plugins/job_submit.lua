-- /etc/slurm/job_submit.lua

local LICENSE_SERVER = "slurmdb"
-- Name passed to spank_job_control_setenv() by the SPANK plugin.
-- slurmctld sees it as-is; "SLURM_SPANK_" is only added in the job's environment.
local QPU_ENV_NAME   = "QRMI_QPU"

-- Strip leading and trailing whitespace.
local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

-- Split a string by a single-character separator (empty items are dropped).
local function split(s, sep)
    local items = {}
    if s == nil then return items end
    for raw in string.gmatch(s, "[^" .. sep .. "]+") do
        local item = trim(raw)
        if item ~= "" then
            table.insert(items, item)
        end
    end
    return items
end

-- Read the --qpu value passed by the SPANK plugin via spank_job_control_setenv().
-- If the variable appears more than once, the last value wins.
local function find_qpu(job_desc)
    local env = job_desc.spank_job_env
    if type(env) ~= "table" then
        return nil
    end
    local prefix = QPU_ENV_NAME .. "="
    local qpu = nil
    for _, kv in ipairs(env) do
        if kv:sub(1, #prefix) == prefix then
            local v = trim(kv:sub(#prefix + 1))
            if v ~= "" then
                qpu = v
            end
        end
    end
    return qpu
end

-- Parse "name[:count]" and return name, count (count defaults to 1).
-- Returns nil if the count is not a valid integer.
local function parse_license(entry)
    local name, count = entry:match("^([^:]+):(%d+)$")
    if name then
        return trim(name), tonumber(count)
    end
    if entry:find(":", 1, true) then
        return nil  -- malformed, e.g. "foo:" or "foo:abc"
    end
    return trim(entry), 1
end

-- Error code for license problems. Falls back to the generic error
-- if this Slurm version does not export ESLURM_INVALID_LICENSES to Lua.
local function license_error()
    return slurm.ESLURM_INVALID_LICENSES or slurm.ERROR
end

function slurm_job_submit(job_desc, part_list, submit_uid)

    -- Jobs without --qpu are not checked.
    local qpu = find_qpu(job_desc)
    if not qpu then
        return slurm.SUCCESS
    end

    slurm.log_info("job_submit: uid=%d detected qpu: %s", submit_uid, qpu)

    -- Build the list of required licenses from --qpu. Slurm stores license
    -- names in lowercase and rejects requests for e.g. PASQAL_LOCAL@slurmdb.
    local required = {}
    for _, host in ipairs(split(qpu, ",")) do
        if not host:match("^[%w_%-]+$") then
            slurm.log_user("Error: invalid --qpu host name '%s'", host)
            return slurm.ERROR
        end
        table.insert(required, host:lower() .. "@" .. LICENSE_SERVER)
    end
    if #required == 0 then
        slurm.log_user("Error: --qpu requires at least one host name")
        return slurm.ERROR
    end

    local licenses = job_desc.licenses or ""

    -- OR'ed licenses ("a|b") allocate only one of the alternatives,
    -- so they can never guarantee every license required by --qpu.
    if licenses:find("|", 1, true) then
        slurm.log_user("Error: --qpu=%s requires all of %s; "
            .. "OR'ed licenses ('|') cannot guarantee this. Use ',' instead.",
            qpu, table.concat(required, ","))
        return license_error()
    end

    -- Collect requested licenses by exact name (AND list).
    local have = {}
    for _, entry in ipairs(split(licenses, ",")) do
        local name, count = parse_license(entry)
        if name == nil then
            slurm.log_user("Error: malformed license specification '%s'", entry)
            return license_error()
        end
        if count >= 1 then
            have[name] = count
        end
    end

    -- Every --qpu host must have a matching license.
    local missing = {}
    for _, name in ipairs(required) do
        if not have[name] then
            table.insert(missing, name .. ":1")
        end
    end

    if #missing > 0 then
        slurm.log_user("Error: --qpu=%s requires --licenses to include %s",
            qpu, table.concat(missing, ","))
        return license_error()
    end

    -- QRMI claims {host}_QRMI_JOB_QPU_SLOTS slots (default 1); it must match
    -- the licenses Slurm grants, or Slurm's count of free slots is wrong.
    -- Only batch jobs send their environment, so srun and salloc are not checked.
    if job_desc.script then
        for _, host in ipairs(split(qpu, ",")) do
            local name = host:lower() .. "@" .. LICENSE_SERVER
            local slots_var = host .. "_QRMI_JOB_QPU_SLOTS"
            local slots = tonumber(job_desc.environment[slots_var] or "1")
            if slots ~= have[name] then
                slurm.log_user("Error: --licenses=%s:%d requires %s=%d",
                    name, have[name], slots_var, have[name])
                return license_error()
            end
        end
    end

    return slurm.SUCCESS
end

function slurm_job_modify(job_desc, job_rec, part_list, modify_uid)
    return slurm.SUCCESS
end
