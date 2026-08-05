"""Forward-only Phase O SQLite schema for servers, bundles, and provisioning."""

# ruff: noqa: E501

PHASE_O_PROVISIONING_UP = (
    """CREATE TABLE server_change_plans(
        plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64 AND plan_digest NOT GLOB '*[^0-9a-f]*'),owner_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN('upsert','set_enabled','set_default','delete')),server_id TEXT NOT NULL,
        changes_json TEXT NOT NULL CHECK(json_valid(changes_json) AND json_type(changes_json)='object' AND length(CAST(changes_json AS BLOB))<=65536),
        expected_revision INTEGER CHECK(expected_revision IS NULL OR expected_revision>=0),impact_json TEXT NOT NULL CHECK(json_valid(impact_json) AND json_type(impact_json)='object' AND length(CAST(impact_json AS BLOB))<=65536),
        created_at TEXT NOT NULL,expires_at TEXT NOT NULL CHECK(julianday(expires_at)>julianday(created_at)),resource_uri TEXT NOT NULL CHECK(resource_uri='comfyui://servers/'||server_id),
        UNIQUE(plan_id,owner_id),UNIQUE(plan_id,plan_digest,owner_id))""",
    "CREATE INDEX ix_server_change_plans_owner ON server_change_plans(owner_id,expires_at,plan_id)",
    "CREATE TRIGGER tr_server_change_plans_immutable_update BEFORE UPDATE ON server_change_plans BEGIN SELECT RAISE(ABORT,'server plan is immutable'); END",
    "CREATE TRIGGER tr_server_change_plans_immutable_delete BEFORE DELETE ON server_change_plans BEGIN SELECT RAISE(ABORT,'server plan is immutable'); END",
    """CREATE TRIGGER tr_server_change_plans_secret_free BEFORE INSERT ON server_change_plans WHEN EXISTS(
        SELECT 1 FROM json_tree(NEW.changes_json) WHERE lower(COALESCE(key,'')) IN('password','passwd','token','secret','api_key','apikey','authorization','auth','cookie','credential','credentials') AND lower(fullkey) NOT LIKE '%secret_refs%')
        BEGIN SELECT RAISE(ABORT,'server plan must not persist secret values'); END""",
    """CREATE TABLE server_revisions(
        server_id TEXT NOT NULL,owner_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN('active','disabled','deleted')),
        config_json TEXT NOT NULL CHECK(json_valid(config_json) AND json_type(config_json)='object' AND length(CAST(config_json AS BLOB))<=65536),
        config_digest TEXT NOT NULL CHECK(length(config_digest)=64 AND config_digest NOT GLOB '*[^0-9a-f]*'),plan_id TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(server_id,owner_id,revision),UNIQUE(server_id,owner_id,revision,config_digest),FOREIGN KEY(plan_id,owner_id) REFERENCES server_change_plans(plan_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_server_revisions_immutable_update BEFORE UPDATE ON server_revisions BEGIN SELECT RAISE(ABORT,'server revision is immutable'); END",
    "CREATE TRIGGER tr_server_revisions_immutable_delete BEFORE DELETE ON server_revisions BEGIN SELECT RAISE(ABORT,'server revision is immutable'); END",
    """CREATE TRIGGER tr_server_revisions_secret_free BEFORE INSERT ON server_revisions WHEN EXISTS(
        SELECT 1 FROM json_tree(NEW.config_json) WHERE lower(COALESCE(key,'')) IN('password','passwd','token','secret','api_key','apikey','authorization','auth','cookie','credential','credentials') AND lower(fullkey) NOT LIKE '%secret_refs%')
        BEGIN SELECT RAISE(ABORT,'server revision must not persist secret values'); END""",
    """CREATE TABLE managed_servers(
        server_id TEXT NOT NULL,owner_id TEXT NOT NULL,current_revision INTEGER NOT NULL CHECK(current_revision>0),current_digest TEXT NOT NULL,
        lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN('active','disabled','deleted')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        PRIMARY KEY(server_id,owner_id),UNIQUE(server_id,owner_id,current_revision,current_digest)) WITHOUT ROWID""",
    "CREATE INDEX ix_managed_servers_owner ON managed_servers(owner_id,lifecycle_status,server_id)",
    """CREATE TRIGGER tr_managed_servers_insert_guard BEFORE INSERT ON managed_servers WHEN NOT EXISTS(
        SELECT 1 FROM server_revisions WHERE server_id=NEW.server_id AND owner_id=NEW.owner_id AND revision=NEW.current_revision AND config_digest=NEW.current_digest AND lifecycle_status=NEW.lifecycle_status)
        BEGIN SELECT RAISE(ABORT,'server current revision is missing'); END""",
    """CREATE TRIGGER tr_managed_servers_update_guard BEFORE UPDATE ON managed_servers WHEN NEW.server_id!=OLD.server_id OR NEW.owner_id!=OLD.owner_id OR OLD.lifecycle_status='deleted' OR NEW.current_revision!=OLD.current_revision+1 OR NOT EXISTS(
        SELECT 1 FROM server_revisions WHERE server_id=NEW.server_id AND owner_id=NEW.owner_id AND revision=NEW.current_revision AND config_digest=NEW.current_digest AND lifecycle_status=NEW.lifecycle_status)
        BEGIN SELECT RAISE(ABORT,'server revision transition conflict'); END""",
    "CREATE TRIGGER tr_managed_servers_delete_guard BEFORE DELETE ON managed_servers BEGIN SELECT RAISE(ABORT,'server history is retained'); END",
    """CREATE TABLE server_defaults(owner_id TEXT PRIMARY KEY NOT NULL,server_id TEXT NOT NULL,server_revision INTEGER NOT NULL,plan_id TEXT NOT NULL,updated_at TEXT NOT NULL,
        FOREIGN KEY(server_id,owner_id,server_revision) REFERENCES server_revisions(server_id,owner_id,revision) ON DELETE RESTRICT,
        FOREIGN KEY(plan_id,owner_id) REFERENCES server_change_plans(plan_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    """CREATE TRIGGER tr_server_defaults_insert_guard BEFORE INSERT ON server_defaults WHEN NOT EXISTS(
        SELECT 1 FROM managed_servers WHERE server_id=NEW.server_id AND owner_id=NEW.owner_id AND current_revision=NEW.server_revision AND lifecycle_status='active')
        BEGIN SELECT RAISE(ABORT,'default server must be active'); END""",
    """CREATE TRIGGER tr_server_defaults_update_guard BEFORE UPDATE ON server_defaults WHEN NEW.owner_id!=OLD.owner_id OR NOT EXISTS(
        SELECT 1 FROM managed_servers WHERE server_id=NEW.server_id AND owner_id=NEW.owner_id AND current_revision=NEW.server_revision AND lifecycle_status='active')
        BEGIN SELECT RAISE(ABORT,'default server transition conflict'); END""",
    """CREATE TABLE server_plan_commits(plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,server_id TEXT NOT NULL,committed_revision INTEGER NOT NULL,result_digest TEXT NOT NULL,committed_at TEXT NOT NULL,
        FOREIGN KEY(plan_id,plan_digest,owner_id) REFERENCES server_change_plans(plan_id,plan_digest,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(server_id,owner_id,committed_revision,result_digest) REFERENCES server_revisions(server_id,owner_id,revision,config_digest) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_server_plan_commits_immutable_update BEFORE UPDATE ON server_plan_commits BEGIN SELECT RAISE(ABORT,'server plan commit is immutable'); END",
    "CREATE TRIGGER tr_server_plan_commits_immutable_delete BEFORE DELETE ON server_plan_commits BEGIN SELECT RAISE(ABORT,'server plan commit is immutable'); END",
    """CREATE TABLE config_state(owner_id TEXT PRIMARY KEY NOT NULL,current_revision INTEGER NOT NULL CHECK(current_revision>0),current_digest TEXT NOT NULL,updated_at TEXT NOT NULL) WITHOUT ROWID""",
    """CREATE TABLE config_workflow_deployments(owner_id TEXT NOT NULL,deployment_id TEXT NOT NULL,server_id TEXT NOT NULL,workflow_id TEXT NOT NULL,
        PRIMARY KEY(owner_id,deployment_id),UNIQUE(owner_id,server_id,workflow_id),
        FOREIGN KEY(server_id,owner_id) REFERENCES managed_servers(server_id,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(deployment_id) REFERENCES workflow_deployments(deployment_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    """CREATE TABLE config_workflow_states(owner_id TEXT NOT NULL,server_id TEXT NOT NULL,workflow_id TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),updated_at TEXT NOT NULL,
        PRIMARY KEY(owner_id,server_id,workflow_id),FOREIGN KEY(owner_id,server_id,workflow_id) REFERENCES config_workflow_deployments(owner_id,server_id,workflow_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE INDEX ix_config_workflow_states_owner ON config_workflow_states(owner_id,server_id,workflow_id)",
    "CREATE TABLE config_workflow_snapshots(owner_id TEXT PRIMARY KEY NOT NULL,updated_at TEXT NOT NULL) WITHOUT ROWID",
    """CREATE TABLE config_bundles(bundle_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL,bundle_version INTEGER NOT NULL CHECK(bundle_version=1),revision INTEGER NOT NULL CHECK(revision>=0),
        content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object' AND length(CAST(content_json AS BLOB))<=1048576),
        content_digest TEXT NOT NULL CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),created_at TEXT NOT NULL,resource_uri TEXT NOT NULL CHECK(resource_uri='comfyui://config/bundles/'||revision),
        UNIQUE(bundle_id,owner_id),UNIQUE(owner_id,revision,content_digest))""",
    "CREATE INDEX ix_config_bundles_owner ON config_bundles(owner_id,revision DESC,bundle_id)",
    "CREATE TRIGGER tr_config_bundles_immutable_update BEFORE UPDATE ON config_bundles BEGIN SELECT RAISE(ABORT,'config bundle is immutable'); END",
    "CREATE TRIGGER tr_config_bundles_immutable_delete BEFORE DELETE ON config_bundles BEGIN SELECT RAISE(ABORT,'config bundle is immutable'); END",
    """CREATE TRIGGER tr_config_bundles_secret_free BEFORE INSERT ON config_bundles WHEN EXISTS(
        SELECT 1 FROM json_tree(NEW.content_json) WHERE lower(COALESCE(key,'')) IN('password','passwd','token','secret','api_key','apikey','authorization','auth','cookie','credential','credentials') AND lower(fullkey) NOT LIKE '%secret_refs%')
        BEGIN SELECT RAISE(ABORT,'config bundle must not persist secret values'); END""",
    """CREATE TABLE config_import_plans(plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,expected_revision INTEGER NOT NULL CHECK(expected_revision>=0),bundle_version INTEGER NOT NULL CHECK(bundle_version=1),
        source_digest TEXT NOT NULL,content_json TEXT NOT NULL CHECK(json_valid(content_json) AND json_type(content_json)='object' AND length(CAST(content_json AS BLOB))<=1048576),
        merge_summary_json TEXT NOT NULL CHECK(json_valid(merge_summary_json) AND length(CAST(merge_summary_json AS BLOB))<=65536),created_at TEXT NOT NULL,expires_at TEXT NOT NULL CHECK(julianday(expires_at)>julianday(created_at)),resource_uri TEXT NOT NULL,
        UNIQUE(plan_id,owner_id),UNIQUE(plan_id,plan_digest,owner_id))""",
    "CREATE TRIGGER tr_config_import_plans_immutable_update BEFORE UPDATE ON config_import_plans BEGIN SELECT RAISE(ABORT,'config import plan is immutable'); END",
    "CREATE TRIGGER tr_config_import_plans_immutable_delete BEFORE DELETE ON config_import_plans BEGIN SELECT RAISE(ABORT,'config import plan is immutable'); END",
    """CREATE TRIGGER tr_config_import_plans_secret_free BEFORE INSERT ON config_import_plans WHEN EXISTS(
        SELECT 1 FROM json_tree(NEW.content_json) WHERE lower(COALESCE(key,'')) IN('password','passwd','token','secret','api_key','apikey','authorization','auth','cookie','credential','credentials') AND lower(fullkey) NOT LIKE '%secret_refs%')
        BEGIN SELECT RAISE(ABORT,'config import plan must not persist secret values'); END""",
    """CREATE TABLE config_import_commits(plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,committed_revision INTEGER NOT NULL,bundle_id TEXT NOT NULL,committed_at TEXT NOT NULL,
        FOREIGN KEY(plan_id,plan_digest,owner_id) REFERENCES config_import_plans(plan_id,plan_digest,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(bundle_id,owner_id) REFERENCES config_bundles(bundle_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_config_import_commits_immutable_update BEFORE UPDATE ON config_import_commits BEGIN SELECT RAISE(ABORT,'config import commit is immutable'); END",
    "CREATE TRIGGER tr_config_import_commits_immutable_delete BEFORE DELETE ON config_import_commits BEGIN SELECT RAISE(ABORT,'config import commit is immutable'); END",
    """CREATE TABLE dependency_plans(plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,server_id TEXT NOT NULL,
        server_revision INTEGER NOT NULL CHECK(server_revision>0),server_config_digest TEXT NOT NULL CHECK(length(server_config_digest)=64),inspection_digest TEXT NOT NULL,
        restart_required INTEGER NOT NULL CHECK(restart_required IN(0,1)),request_confirmation TEXT NOT NULL CHECK(request_confirmation='INSTALL APPROVED DEPENDENCIES'),
        created_at TEXT NOT NULL,expires_at TEXT NOT NULL CHECK(julianday(expires_at)>julianday(created_at)),resource_uri TEXT NOT NULL CHECK(resource_uri='comfyui://dependencies/plans/'||plan_id),
        UNIQUE(plan_id,owner_id),UNIQUE(plan_id,plan_digest,owner_id),FOREIGN KEY(server_id,owner_id,server_revision,server_config_digest) REFERENCES server_revisions(server_id,owner_id,revision,config_digest) ON DELETE RESTRICT)""",
    "CREATE INDEX ix_dependency_plans_owner ON dependency_plans(owner_id,expires_at,plan_id)",
    "CREATE TRIGGER tr_dependency_plans_immutable_update BEFORE UPDATE ON dependency_plans BEGIN SELECT RAISE(ABORT,'dependency plan is immutable'); END",
    "CREATE TRIGGER tr_dependency_plans_immutable_delete BEFORE DELETE ON dependency_plans BEGIN SELECT RAISE(ABORT,'dependency plan is immutable'); END",
    """CREATE TABLE dependency_plan_items(plan_id TEXT NOT NULL,owner_id TEXT NOT NULL,item_id TEXT NOT NULL,ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 511),dependency_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN('node','model')),source_type TEXT NOT NULL CHECK(source_type IN('git','model')),source_url TEXT NOT NULL CHECK(source_url LIKE 'https://%' AND instr(source_url,'@')=0 AND instr(source_url,'#')=0),
        version TEXT NOT NULL,checksum TEXT NOT NULL CHECK(length(checksum)=64 AND checksum NOT GLOB '*[^0-9a-f]*'),size_bytes INTEGER NOT NULL CHECK(size_bytes>0 AND size_bytes<=21474836480 AND(kind!='node' OR size_bytes<=536870912)),
        target_dir TEXT NOT NULL,license TEXT NOT NULL,restart_required INTEGER NOT NULL CHECK(restart_required IN(0,1)),install_state TEXT NOT NULL CHECK(install_state IN('missing','installed','update_available')),
        PRIMARY KEY(plan_id,item_id),UNIQUE(plan_id,ordinal),FOREIGN KEY(plan_id,owner_id) REFERENCES dependency_plans(plan_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_dependency_plan_items_immutable_update BEFORE UPDATE ON dependency_plan_items BEGIN SELECT RAISE(ABORT,'dependency plan item is immutable'); END",
    "CREATE TRIGGER tr_dependency_plan_items_immutable_delete BEFORE DELETE ON dependency_plan_items BEGIN SELECT RAISE(ABORT,'dependency plan item is immutable'); END",
    """CREATE TRIGGER tr_dependency_plan_items_exact_version BEFORE INSERT ON dependency_plan_items WHEN NOT(
        (length(NEW.version)=40 AND NEW.version NOT GLOB '*[^0-9a-f]*') OR(NEW.version LIKE 'tag:%' AND length(NEW.version)<=128 AND NEW.version NOT LIKE '%..%'))
        BEGIN SELECT RAISE(ABORT,'dependency version must be an exact commit or tag'); END""",
    """CREATE TABLE approvals(approval_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL,operation TEXT NOT NULL CHECK(operation='dependency.install'),plan_id TEXT NOT NULL,plan_digest TEXT NOT NULL,
        impact_summary_json TEXT NOT NULL CHECK(json_valid(impact_summary_json) AND length(CAST(impact_summary_json AS BLOB))<=65536),single_use INTEGER NOT NULL CHECK(single_use=1),revision INTEGER NOT NULL CHECK(revision=0),
        created_at TEXT NOT NULL,expires_at TEXT NOT NULL CHECK(julianday(expires_at)>julianday(created_at)),resource_uri TEXT NOT NULL CHECK(resource_uri='comfyui://approvals/'||approval_id),UNIQUE(approval_id,owner_id),
        FOREIGN KEY(plan_id,plan_digest,owner_id) REFERENCES dependency_plans(plan_id,plan_digest,owner_id) ON DELETE RESTRICT)""",
    "CREATE TRIGGER tr_approvals_immutable_update BEFORE UPDATE ON approvals BEGIN SELECT RAISE(ABORT,'approval is immutable'); END",
    "CREATE TRIGGER tr_approvals_immutable_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT,'approval is immutable'); END",
    """CREATE TABLE approval_decision_plans(approval_plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,approval_id TEXT NOT NULL,owner_id TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),
        reason TEXT NOT NULL CHECK(length(reason)<=512),approval_revision INTEGER NOT NULL CHECK(approval_revision=0),status_before TEXT NOT NULL CHECK(status_before='pending'),created_at TEXT NOT NULL,expires_at TEXT NOT NULL CHECK(julianday(expires_at)>julianday(created_at)),resource_uri TEXT NOT NULL,
        UNIQUE(approval_plan_id,owner_id),UNIQUE(approval_plan_id,plan_digest,owner_id),FOREIGN KEY(approval_id,owner_id) REFERENCES approvals(approval_id,owner_id) ON DELETE RESTRICT)""",
    "CREATE TRIGGER tr_approval_decision_plans_immutable_update BEFORE UPDATE ON approval_decision_plans BEGIN SELECT RAISE(ABORT,'approval decision plan is immutable'); END",
    "CREATE TRIGGER tr_approval_decision_plans_immutable_delete BEFORE DELETE ON approval_decision_plans BEGIN SELECT RAISE(ABORT,'approval decision plan is immutable'); END",
    """CREATE TABLE approval_decisions(approval_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL,approval_plan_id TEXT NOT NULL UNIQUE,decision TEXT NOT NULL CHECK(decision IN('approved','rejected')),reason TEXT NOT NULL,decided_at TEXT NOT NULL,UNIQUE(approval_id,owner_id),
        FOREIGN KEY(approval_id,owner_id) REFERENCES approvals(approval_id,owner_id) ON DELETE RESTRICT,FOREIGN KEY(approval_plan_id,owner_id) REFERENCES approval_decision_plans(approval_plan_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_approval_decisions_immutable_update BEFORE UPDATE ON approval_decisions BEGIN SELECT RAISE(ABORT,'approval decision is immutable'); END",
    "CREATE TRIGGER tr_approval_decisions_immutable_delete BEFORE DELETE ON approval_decisions BEGIN SELECT RAISE(ABORT,'approval decision is immutable'); END",
    """CREATE TABLE approval_decision_commits(approval_plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,approval_id TEXT NOT NULL UNIQUE,committed_at TEXT NOT NULL,
        FOREIGN KEY(approval_plan_id,plan_digest,owner_id) REFERENCES approval_decision_plans(approval_plan_id,plan_digest,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(approval_id,owner_id) REFERENCES approval_decisions(approval_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_approval_decision_commits_immutable_update BEFORE UPDATE ON approval_decision_commits BEGIN SELECT RAISE(ABORT,'approval decision commit is immutable'); END",
    "CREATE TRIGGER tr_approval_decision_commits_immutable_delete BEFORE DELETE ON approval_decision_commits BEGIN SELECT RAISE(ABORT,'approval decision commit is immutable'); END",
    """CREATE TABLE provisioning_jobs(job_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL,plan_id TEXT NOT NULL,plan_digest TEXT NOT NULL,approval_id TEXT NOT NULL,request_id TEXT NOT NULL,server_id TEXT NOT NULL,
        server_revision INTEGER NOT NULL CHECK(server_revision>0),server_config_digest TEXT NOT NULL CHECK(length(server_config_digest)=64),status TEXT NOT NULL CHECK(status IN('pending','running','completed','failed','cancelled')),restart_required INTEGER NOT NULL CHECK(restart_required IN(0,1)),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,
        resource_uri TEXT NOT NULL CHECK(resource_uri='comfyui://provisioning/jobs/'||job_id),UNIQUE(job_id,owner_id),UNIQUE(owner_id,request_id),
        FOREIGN KEY(plan_id,plan_digest,owner_id) REFERENCES dependency_plans(plan_id,plan_digest,owner_id) ON DELETE RESTRICT,FOREIGN KEY(approval_id,owner_id) REFERENCES approvals(approval_id,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(server_id,owner_id,server_revision,server_config_digest) REFERENCES server_revisions(server_id,owner_id,revision,config_digest) ON DELETE RESTRICT,
        CHECK((status IN('completed','failed','cancelled'))=(completed_at IS NOT NULL)))""",
    "CREATE INDEX ix_provisioning_jobs_owner ON provisioning_jobs(owner_id,status,updated_at,job_id)",
    """CREATE TRIGGER tr_provisioning_jobs_update_guard BEFORE UPDATE ON provisioning_jobs WHEN NEW.job_id!=OLD.job_id OR NEW.owner_id!=OLD.owner_id OR NEW.plan_id!=OLD.plan_id OR NEW.plan_digest!=OLD.plan_digest OR NEW.approval_id!=OLD.approval_id OR NEW.request_id!=OLD.request_id OR NEW.server_id!=OLD.server_id OR NEW.server_revision!=OLD.server_revision OR NEW.server_config_digest!=OLD.server_config_digest OR NEW.restart_required!=OLD.restart_required OR NOT(
        NEW.status=OLD.status OR(OLD.status='pending' AND NEW.status IN('running','cancelled','failed')) OR(OLD.status='running' AND NEW.status IN('completed','failed','cancelled')))
        BEGIN SELECT RAISE(ABORT,'provisioning job transition conflict'); END""",
    "CREATE TRIGGER tr_provisioning_jobs_delete_guard BEFORE DELETE ON provisioning_jobs BEGIN SELECT RAISE(ABORT,'provisioning job history is retained'); END",
    """CREATE TABLE approval_uses(approval_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL,plan_id TEXT NOT NULL,plan_digest TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,used_at TEXT NOT NULL,
        FOREIGN KEY(approval_id,owner_id) REFERENCES approval_decisions(approval_id,owner_id) ON DELETE RESTRICT,FOREIGN KEY(plan_id,plan_digest,owner_id) REFERENCES dependency_plans(plan_id,plan_digest,owner_id) ON DELETE RESTRICT,
        FOREIGN KEY(job_id,owner_id) REFERENCES provisioning_jobs(job_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    """CREATE TRIGGER tr_approval_uses_binding BEFORE INSERT ON approval_uses WHEN NOT EXISTS(
        SELECT 1 FROM approvals a JOIN approval_decisions d ON d.approval_id=a.approval_id AND d.owner_id=a.owner_id WHERE a.approval_id=NEW.approval_id AND a.owner_id=NEW.owner_id AND a.plan_id=NEW.plan_id AND a.plan_digest=NEW.plan_digest AND d.decision='approved' AND julianday(a.expires_at)>julianday(NEW.used_at))
        BEGIN SELECT RAISE(ABORT,'approval use binding conflict'); END""",
    "CREATE TRIGGER tr_approval_uses_immutable_update BEFORE UPDATE ON approval_uses BEGIN SELECT RAISE(ABORT,'approval use is immutable'); END",
    "CREATE TRIGGER tr_approval_uses_immutable_delete BEFORE DELETE ON approval_uses BEGIN SELECT RAISE(ABORT,'approval use is immutable'); END",
    """CREATE TABLE provisioning_install_items(job_id TEXT NOT NULL,owner_id TEXT NOT NULL,item_id TEXT NOT NULL,ordinal INTEGER NOT NULL,kind TEXT NOT NULL,source_type TEXT NOT NULL,source_url TEXT NOT NULL,version TEXT NOT NULL,checksum TEXT NOT NULL,size_bytes INTEGER NOT NULL,target_dir TEXT NOT NULL,restart_required INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,status TEXT NOT NULL CHECK(status IN('pending','enqueuing','queued','running','completed','failed','cancelled')),
        current_checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(current_checkpoint_json) AND length(CAST(current_checkpoint_json AS BLOB))<=65536),current_checkpoint_digest TEXT,
        lease_work_item_id TEXT,lease_worker_id TEXT,lease_token INTEGER,updated_at TEXT NOT NULL,completed_at TEXT,PRIMARY KEY(job_id,item_id),UNIQUE(job_id,ordinal),
        FOREIGN KEY(job_id,owner_id) REFERENCES provisioning_jobs(job_id,owner_id) ON DELETE RESTRICT,CHECK((status IN('completed','failed','cancelled'))=(completed_at IS NOT NULL))) WITHOUT ROWID""",
    """CREATE TRIGGER tr_provisioning_items_update_guard BEFORE UPDATE ON provisioning_install_items WHEN NEW.job_id!=OLD.job_id OR NEW.owner_id!=OLD.owner_id OR NEW.item_id!=OLD.item_id OR NEW.ordinal!=OLD.ordinal OR NEW.kind!=OLD.kind OR NEW.source_type!=OLD.source_type OR NEW.source_url!=OLD.source_url OR NEW.version!=OLD.version OR NEW.checksum!=OLD.checksum OR NEW.size_bytes!=OLD.size_bytes OR NEW.target_dir!=OLD.target_dir OR NEW.restart_required!=OLD.restart_required OR NEW.idempotency_key!=OLD.idempotency_key OR NOT(
        NEW.status=OLD.status OR(OLD.status='pending' AND NEW.status IN('enqueuing','cancelled')) OR(OLD.status='enqueuing' AND NEW.status IN('enqueuing','queued','running','completed','failed','cancelled')) OR(OLD.status='queued' AND NEW.status IN('running','completed','failed','cancelled')) OR(OLD.status='running' AND NEW.status IN('completed','failed','cancelled')))
        BEGIN SELECT RAISE(ABORT,'provisioning item transition conflict'); END""",
    "CREATE TRIGGER tr_provisioning_items_delete_guard BEFORE DELETE ON provisioning_install_items BEGIN SELECT RAISE(ABORT,'provisioning item history is retained'); END",
    """CREATE TABLE provisioning_item_checkpoints(checkpoint_id TEXT PRIMARY KEY NOT NULL,job_id TEXT NOT NULL,owner_id TEXT NOT NULL,item_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),status TEXT NOT NULL CHECK(status IN('enqueuing','queued','running','completed','failed','cancelled')),
        checkpoint_json TEXT NOT NULL CHECK(json_valid(checkpoint_json) AND length(CAST(checkpoint_json AS BLOB))<=65536),checkpoint_digest TEXT NOT NULL,observed_at TEXT NOT NULL,
        UNIQUE(job_id,item_id,sequence),UNIQUE(job_id,item_id,checkpoint_digest),FOREIGN KEY(job_id,item_id) REFERENCES provisioning_install_items(job_id,item_id) ON DELETE RESTRICT)""",
    "CREATE TRIGGER tr_provisioning_checkpoints_immutable_update BEFORE UPDATE ON provisioning_item_checkpoints BEGIN SELECT RAISE(ABORT,'provisioning checkpoint is append-only'); END",
    "CREATE TRIGGER tr_provisioning_checkpoints_immutable_delete BEFORE DELETE ON provisioning_item_checkpoints BEGIN SELECT RAISE(ABORT,'provisioning checkpoint is append-only'); END",
    """CREATE TABLE provisioning_cancel_plans(cancel_plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,job_id TEXT NOT NULL,impact_json TEXT NOT NULL CHECK(json_valid(impact_json) AND length(CAST(impact_json AS BLOB))<=65536),created_at TEXT NOT NULL,expires_at TEXT NOT NULL CHECK(julianday(expires_at)>julianday(created_at)),resource_uri TEXT NOT NULL,
        UNIQUE(cancel_plan_id,owner_id),UNIQUE(cancel_plan_id,plan_digest,owner_id),FOREIGN KEY(job_id,owner_id) REFERENCES provisioning_jobs(job_id,owner_id) ON DELETE RESTRICT)""",
    "CREATE TRIGGER tr_provisioning_cancel_plans_immutable_update BEFORE UPDATE ON provisioning_cancel_plans BEGIN SELECT RAISE(ABORT,'provisioning cancel plan is immutable'); END",
    "CREATE TRIGGER tr_provisioning_cancel_plans_immutable_delete BEFORE DELETE ON provisioning_cancel_plans BEGIN SELECT RAISE(ABORT,'provisioning cancel plan is immutable'); END",
    """CREATE TABLE provisioning_cancel_commits(cancel_plan_id TEXT PRIMARY KEY NOT NULL,plan_digest TEXT NOT NULL,owner_id TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE,committed_at TEXT NOT NULL,
        FOREIGN KEY(cancel_plan_id,plan_digest,owner_id) REFERENCES provisioning_cancel_plans(cancel_plan_id,plan_digest,owner_id) ON DELETE RESTRICT,FOREIGN KEY(job_id,owner_id) REFERENCES provisioning_jobs(job_id,owner_id) ON DELETE RESTRICT) WITHOUT ROWID""",
    "CREATE TRIGGER tr_provisioning_cancel_commits_immutable_update BEFORE UPDATE ON provisioning_cancel_commits BEGIN SELECT RAISE(ABORT,'provisioning cancel commit is immutable'); END",
    "CREATE TRIGGER tr_provisioning_cancel_commits_immutable_delete BEFORE DELETE ON provisioning_cancel_commits BEGIN SELECT RAISE(ABORT,'provisioning cancel commit is immutable'); END",
    """CREATE TABLE phase_o_audit_events(event_id TEXT PRIMARY KEY NOT NULL,owner_id TEXT NOT NULL,event_type TEXT NOT NULL,subject_uri TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),correlation_id TEXT NOT NULL,
        data_json TEXT NOT NULL CHECK(json_valid(data_json) AND length(CAST(data_json AS BLOB))<=65536),data_digest TEXT NOT NULL,occurred_at TEXT NOT NULL,UNIQUE(subject_uri,sequence))""",
    "CREATE TRIGGER tr_phase_o_audit_immutable_update BEFORE UPDATE ON phase_o_audit_events BEGIN SELECT RAISE(ABORT,'phase O audit event is append-only'); END",
    "CREATE TRIGGER tr_phase_o_audit_immutable_delete BEFORE DELETE ON phase_o_audit_events BEGIN SELECT RAISE(ABORT,'phase O audit event is append-only'); END",
    """CREATE TRIGGER tr_phase_o_audit_secret_free BEFORE INSERT ON phase_o_audit_events WHEN EXISTS(
        SELECT 1 FROM json_tree(NEW.data_json) WHERE lower(COALESCE(key,'')) IN('password','passwd','token','secret','api_key','apikey','authorization','auth','cookie','credential','credentials'))
        BEGIN SELECT RAISE(ABORT,'audit event must not persist secret values'); END""",
    """CREATE TABLE phase_o_outbox(outbox_id TEXT PRIMARY KEY NOT NULL,event_id TEXT NOT NULL UNIQUE REFERENCES phase_o_audit_events(event_id) ON DELETE RESTRICT,owner_id TEXT NOT NULL,topic TEXT NOT NULL CHECK(topic='resource.updated'),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=65536),payload_digest TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN('pending','delivered')),created_at TEXT NOT NULL,delivered_at TEXT,CHECK((status='delivered')=(delivered_at IS NOT NULL)))""",
    "CREATE INDEX ix_phase_o_outbox_pending ON phase_o_outbox(owner_id,created_at,outbox_id) WHERE status='pending'",
    """CREATE TRIGGER tr_phase_o_outbox_update_guard BEFORE UPDATE ON phase_o_outbox WHEN NEW.outbox_id!=OLD.outbox_id OR NEW.event_id!=OLD.event_id OR NEW.owner_id!=OLD.owner_id OR NEW.topic!=OLD.topic OR NEW.payload_json!=OLD.payload_json OR NEW.payload_digest!=OLD.payload_digest OR NEW.created_at!=OLD.created_at OR NOT(OLD.status='pending' AND NEW.status='delivered' AND OLD.delivered_at IS NULL AND NEW.delivered_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'phase O outbox transition conflict'); END""",
    "CREATE TRIGGER tr_phase_o_outbox_delete_guard BEFORE DELETE ON phase_o_outbox BEGIN SELECT RAISE(ABORT,'phase O outbox history is retained'); END",
)
